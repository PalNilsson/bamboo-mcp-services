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
        (``<logical>__a``) is recorded and returned.

        Args:
            logical: Logical collection name (e.g. ``"atlas_docs"``).

        Returns:
            Physical ChromaDB collection name.
        """
        if logical not in self._data:
            self._data[logical] = f"{logical}__a"
            self._save()
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
        2. Delete all documents from the old live collection (so it is clean
           for the next update cycle), then delete the collection itself.
        3. Write the updated routing record to the sidecar via
           ``write + os.replace``.

        After this call :meth:`live_name` returns the former idle name.

        Args:
            logical: Logical collection name.
            chroma: :class:`ChromaWrapper` instance used to delete the old
                live collection.
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

        # Clean up the old live collection now that it is no longer routed.
        chroma.delete_collection(old_live)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Load routing data from the sidecar file if it exists."""
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text(encoding="utf-8"))
            except Exception:
                LOG.warning(
                    "CollectionRouter: failed to read sidecar '%s'; starting fresh.",
                    self._path,
                )
                self._data = {}

    def _save(self) -> None:
        """Persist routing data atomically via write + os.replace."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)


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
