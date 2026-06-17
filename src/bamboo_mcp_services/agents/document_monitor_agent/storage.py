"""ChromaDB wrapper utilities and blue/green collection router.

Two concerns live here:

1. :class:`ChromaWrapper` — a thin adapter around ``chromadb`` that normalises
   the API differences between the legacy (<0.4) and modern (>=0.4) client
   releases.

2. :class:`CollectionRouter` — a JSON-backed indirection layer that maps a
   *logical* collection name to one of two *physical* ChromaDB collection
   names (the blue/green slots ``<name>__a`` and ``<name>__b``).  Only the
   current live slot is queried by readers; the idle slot is used as the build
   target for the next update.  Once the build is complete the router atomically
   writes the new routing record (via ``os.replace``) and the live slot switches
   with zero reader downtime.

Blue/green swap sequence
------------------------
::

    idle_col  = router.idle_collection(logical)   # returns the currently-unused slot
    # ... populate idle_col with new chunks ...
    router.commit_swap(logical, chroma_wrapper)   # atomic JSON sidecar update
                                                   # + delete old slot's data

The routing sidecar is written with a write-then-``os.replace`` pattern so the
file is never partially written from a reader's perspective.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Dict, List, Optional

import chromadb
from chromadb.config import Settings
from chromadb.api import Collection

LOG = logging.getLogger(__name__)

#: The two physical slot suffixes used for blue/green rotation.
_SLOTS = ("__a", "__b")


class CollectionRouter:
    """Maps a logical ChromaDB collection name to a physical blue/green slot.

    The mapping is persisted in a small JSON sidecar file.  Reads of the sidecar
    are done lazily (on first access and after each swap); writes use
    ``os.replace`` for atomicity.

    The sidecar format is a JSON object whose keys are logical names and whose
    values are the currently-live physical collection names::

        {
            "atlas_docs": "atlas_docs__a",
            "epic_docs":  "epic_docs__b"
        }

    Args:
        sidecar_path: Filesystem path for the JSON routing sidecar.
    """

    def __init__(self, sidecar_path: str) -> None:
        self._path = Path(sidecar_path)
        self._data: Dict[str, str] = {}
        self._load()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def live_name(self, logical: str) -> str:
        """Return the physical collection name currently serving *logical*.

        If no routing record exists for *logical*, the default live slot
        (``<logical>__a``) is used **in memory only** — the sidecar is *not*
        written until :meth:`commit_swap` completes successfully.  This prevents
        a race where the sidecar records ``__a`` as live before any ingestion has
        occurred, which would leave readers pointing at an empty collection if the
        process crashes before the first swap.

        Args:
            logical: Logical collection name (e.g. ``"atlas_docs"``).

        Returns:
            Physical ChromaDB collection name.
        """
        if logical not in self._data:
            # Lazy per-key load: read only this instance's own entry from the
            # sidecar.  Loading the full sidecar into self._data would cause a
            # multi-agent clobber (see _load() docstring for the full story).
            try:
                on_disk = json.loads(self._path.read_text(encoding="utf-8"))
                self._data[logical] = on_disk.get(logical, f"{logical}__a")
            except (FileNotFoundError, json.JSONDecodeError):
                self._data[logical] = f"{logical}__a"
            # Intentionally NOT calling _save() here.  The sidecar must only
            # reflect slots that contain committed, populated data.  The first
            # write happens inside commit_swap() once ingestion has succeeded.
        return self._data[logical]

    def idle_name(self, logical: str) -> str:
        """Return the physical collection name of the currently-idle slot.

        The idle slot is the one *not* currently serving queries — it is safe
        to write into without affecting readers.

        Args:
            logical: Logical collection name.

        Returns:
            Physical ChromaDB collection name for the idle slot.
        """
        live = self.live_name(logical)
        for suffix in _SLOTS:
            candidate = f"{logical}{suffix}"
            if candidate != live:
                return candidate
        # Unreachable if _SLOTS has exactly two entries, but be safe.
        return f"{logical}__b"

    def commit_swap(self, logical: str, chroma: "ChromaWrapper") -> None:
        """Atomically promote the idle slot to be the new live slot.

        Steps:

        1. Determine the current live and idle physical names.
        2. Write the updated routing record to the sidecar via
           ``write + os.replace`` so readers atomically see the new slot.
        3. Verify the newly live collection contains at least one document.
           If it is empty, the sidecar write is *not* rolled back (the slot is
           genuinely live and will be populated on the next ingestion cycle), but
           a prominent WARNING is emitted so operators can investigate.
        4. Delete the old live collection now that it is no longer routed.

        After this call :meth:`live_name` returns the former idle name.

        Args:
            logical: Logical collection name.
            chroma: :class:`ChromaWrapper` instance used to verify and clean up
                the collections.

        Raises:
            RuntimeError: If the newly promoted collection reports zero
                documents.  The sidecar has already been written at this point
                (the slot is live); the error is raised so the caller can log it
                as a fatal cycle failure.
        """
        old_live = self.live_name(logical)
        new_live = self.idle_name(logical)

        # Update the in-memory record and persist atomically.
        self._data[logical] = new_live
        self._save()
        LOG.info(
            "CollectionRouter: swapped '%s': %s → %s",
            logical, old_live, new_live,
        )

        # Post-swap invariant: the newly live collection must be non-empty.
        # An empty collection here means ingestion completed with zero chunks,
        # which breaks all RAG queries for this logical collection.
        new_count = chroma.collection_count(new_live)
        if new_count == 0:
            raise RuntimeError(
                f"CollectionRouter invariant violated after swap: '{logical}' → "
                f"'{new_live}' contains 0 documents.  Ingestion produced no "
                f"chunks — check embedder and source files.  Sidecar already "
                f"updated; re-run ingestion to populate this slot."
            )
        LOG.info(
            "CollectionRouter: invariant OK — '%s' (%s) has %d document(s).",
            logical, new_live, new_count,
        )

        # Clean up the old live collection now that it is no longer routed.
        chroma.delete_collection(old_live)

    def verify_routing_invariant(self, chroma: "ChromaWrapper") -> list[str]:
        """Check that every sidecar entry points at a non-empty collection.

        This is a diagnostic / end-of-cycle health check.  It does **not**
        modify any state — it only reads collection counts and reports problems.

        The key invariant::

            For every logical name L in the sidecar, the physical collection
            ``sidecar[L]`` must exist in ChromaDB and contain > 0 documents.

        Args:
            chroma: :class:`ChromaWrapper` used to query collection counts.

        Returns:
            A list of human-readable ``"[STATUS] logical -> physical (N docs)"``
            strings, one per entry.  Entries that pass the invariant are
            prefixed ``[OK]``; broken entries are prefixed ``[BROKEN]``.

        Raises:
            RuntimeError: If any entry is broken (non-zero count or missing
                collection).  The error message lists all broken entries.
        """
        lines: list[str] = []
        broken: list[str] = []

        counts = chroma.all_collection_counts()

        for logical, physical in sorted(self._data.items()):
            count = counts.get(physical, "MISSING")
            if isinstance(count, int) and count > 0:
                status = "OK"
                lines.append(f"[{status}] {logical} -> {physical}  ({count} docs)")
            else:
                status = "BROKEN"
                entry = f"[{status}] {logical} -> {physical}  ({count} docs)"
                lines.append(entry)
                broken.append(entry)

        for line in lines:
            if line.startswith("[OK]"):
                LOG.info("routing: %s", line)
            else:
                LOG.error("routing: %s", line)

        if broken:
            raise RuntimeError(
                "collection_routing.json invariant violated:\n"
                + "\n".join(broken)
            )

        return lines

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Intentional no-op — individual entries are loaded lazily in live_name().

        Background: the previous implementation loaded the *entire* sidecar into
        ``self._data`` here.  That caused a subtle multi-agent clobber bug:

        * Five :class:`DocumentMonitorAgent` instances each own one logical name
          and share one sidecar path.  Each creates its own
          :class:`CollectionRouter`.
        * At startup every instance called ``_load()``, which populated
          ``self._data`` with **all five** entries from the on-disk sidecar.
        * If agent A swapped its slot (writing ``bamboo_docs__a`` to disk) and
          then agent B subsequently called ``commit_swap`` for *its own* name,
          ``_save()`` would call ``merged.update(self._data)``.  Because
          ``self._data`` still held the **pre-swap** value ``bamboo_docs__b``
          (loaded at B's startup, before A's swap), that stale value overwrote
          A's freshly written ``bamboo_docs__a`` in the on-disk sidecar.

        The fix is to keep ``self._data`` scoped to only the entries **this
        instance owns** (i.e. the logical names it has explicitly set via
        :meth:`live_name` or :meth:`commit_swap`).  Foreign entries are
        preserved through the read-modify-write in :meth:`_save` — they are
        loaded from disk immediately before each write and the overlay
        ``merged.update(self._data)`` only overwrites entries this instance
        controls.

        Per-key lookup from disk is done lazily inside :meth:`live_name` the
        first time a logical name is accessed, so the correct persisted slot is
        still recovered after a restart.
        """
        # Intentionally empty: self._data starts as {} and is populated lazily.

    def _save(self) -> None:
        """Persist routing data atomically via read-modify-write + os.replace.

        Each call re-reads the on-disk sidecar immediately before writing so
        that concurrent :class:`CollectionRouter` instances (one per
        :class:`~bamboo_mcp_services.agents.document_monitor_agent.agent.DocumentMonitorAgent`)
        do not clobber each other's entries.  Without this merge step the last
        writer wins and the sidecar ends up with only a single entry — the
        collection that most recently completed its swap.

        The merge rule is: on-disk entries are loaded first, then *this*
        instance's in-memory ``_data`` is overlaid on top, so a freshly swapped
        entry always takes precedence over any stale on-disk value for the same
        logical name.

        ``self._data`` is intentionally **not** updated with the merged result
        after the write.  Keeping ``self._data`` scoped to only this instance's
        own entries is what prevents the multi-agent clobber bug — see
        :meth:`_load` for the full explanation.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)

        # Read-modify-write: load whatever is on disk right now, then overlay
        # our in-memory state so our latest entry wins for our own logical names
        # while preserving every entry written by other router instances.
        merged: Dict[str, str] = {}
        try:
            merged = json.loads(self._path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            pass  # first write, or corrupt file — start from empty

        merged.update(self._data)

        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(merged, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)

        # NOTE: self._data is NOT updated to merged here.  Doing so would
        # re-introduce foreign entries into this instance's _data, causing
        # a subsequent _save() to overwrite those foreign entries with
        # whatever stale values they had when this instance loaded the sidecar.


class ChromaWrapper:
    """Small wrapper around chromadb.Client to centralise creation and persistence.

    This wrapper attempts to use the Settings-based client construction
    (recommended).  If the installed chromadb package refuses that configuration
    (legacy vs new API), it falls back to a simpler client() call (best-effort).
    When falling back, persistent storage behaviour may differ depending on the
    installed chromadb version.
    """

    def __init__(self, persist_directory: str = ".chromadb", settings_kwargs: Optional[Dict] = None) -> None:
        """Initialize the Chroma client.

        Args:
            persist_directory: Local directory where Chroma will persist data.
            settings_kwargs: Optional extra kwargs forwarded to Settings.
        """
        settings_kwargs = settings_kwargs or {}
        try:
            # Preferred: modern API available since chromadb 0.4
            self.client = chromadb.PersistentClient(path=persist_directory)
            LOG.info("Created chromadb.PersistentClient (persist_directory=%s)", persist_directory)
        except AttributeError:
            # Fallback for older chromadb (<0.4) that uses Settings-based construction
            LOG.debug("chromadb.PersistentClient not available; falling back to legacy Settings-based client.")
            try:
                settings = Settings(
                    chroma_db_impl="duckdb+parquet",
                    persist_directory=persist_directory,
                    **settings_kwargs,
                )
                self.client = chromadb.Client(settings=settings)
                LOG.info("Created chromadb.Client using legacy Settings (persist_directory=%s)", persist_directory)
            except Exception as exc2:
                LOG.exception("Failed to create chromadb client: %s", exc2)
                raise

    def get_or_create_collection(self, name: str) -> Collection:
        """Get or create a collection.

        Args:
            name: Collection name.

        Returns:
            chromadb.api.Collection instance
        """
        return self.client.get_or_create_collection(name)

    def create_collection(self, name: str) -> Collection:
        """Create a brand-new collection with *name*.

        Always calls ``client.create_collection`` rather than
        ``get_or_create_collection``.  This guarantees that no embedding
        dimension is inherited from a previous incarnation of the collection,
        which is the root cause of ChromaDB dimension-mismatch errors when the
        embedder model changes between runs.

        The caller is responsible for deleting any pre-existing collection with
        the same name before calling this method (use :meth:`delete_collection`).

        Args:
            name: Collection name.

        Returns:
            chromadb.api.Collection instance
        """
        return self.client.create_collection(name)

    def delete_collection(self, name: str) -> None:
        """Delete a collection by name (best-effort; logs and swallows errors).

        Args:
            name: Collection name to delete.
        """
        try:
            self.client.delete_collection(name)
            LOG.debug("Deleted chroma collection '%s'.", name)
        except Exception:
            LOG.debug("Could not delete chroma collection '%s' (may not exist).", name, exc_info=True)

    def add_documents(
        self,
        collection: Collection,
        ids: List[str],
        documents: List[str],
        metadatas: List[Dict],
        embeddings: Optional[List[List[float]]] = None,
    ) -> None:
        """Add documents to the provided collection.

        Args:
            collection: Chromadb collection instance.
            ids: List of deterministic IDs.
            documents: List of document text bodies.
            metadatas: List of metadata dictionaries.
            embeddings: Optional list of embeddings (if provided, they must align).
        """
        if embeddings is None:
            collection.add(ids=ids, documents=documents, metadatas=metadatas)
        else:
            collection.add(ids=ids, documents=documents, metadatas=metadatas, embeddings=embeddings)

    def collection_count(self, name: str) -> int:
        """Return the number of documents in *name*, or 0 if it does not exist.

        Args:
            name: Physical collection name.

        Returns:
            Document count, or 0 if the collection is absent or unreadable.
        """
        try:
            col = self.client.get_collection(name)
            return col.count()
        except Exception:
            return 0

    def all_collection_counts(self) -> Dict[str, int]:
        """Return a mapping of physical collection name → document count.

        Used by :meth:`CollectionRouter.verify_routing_invariant` to snapshot
        all collection sizes in a single pass.

        Returns:
            Dict mapping collection name to its document count.
        """
        try:
            return {col.name: col.count() for col in self.client.list_collections()}
        except Exception:
            LOG.warning("ChromaWrapper: could not list collections for count snapshot.")
            return {}

    def delete_documents_by_ids(self, collection: Collection, ids: List[str]) -> None:
        """Delete documents from a collection by their ids (best-effort).

        Different chromadb releases expose different APIs for deletion; attempt
        the common ``collection.delete(ids=...)`` and fall back gracefully.
        """
        if not ids:
            return
        try:
            collection.delete(ids=ids)
            LOG.debug("Deleted %d documents from chroma collection.", len(ids))
            return
        except Exception:
            LOG.debug("collection.delete(ids=...) failed; trying per-id delete", exc_info=True)

        for _id in ids:
            try:
                collection.delete(ids=[_id])
            except Exception:
                LOG.exception("Failed to delete id %s from chroma collection (best-effort)", _id)

    def persist(self) -> None:
        """Persist the client's state to disk if supported.

        chromadb.PersistentClient (>=0.4) persists automatically on every write,
        so this is a no-op for modern versions.  Kept for compatibility with older
        clients that require an explicit persist() call.
        """
        try:
            self.client.persist()
        except Exception:
            LOG.debug("Chroma persist() not supported in this client release (safe to ignore).", exc_info=True)
