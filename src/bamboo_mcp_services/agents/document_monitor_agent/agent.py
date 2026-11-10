"""Document monitor agent implementation.

This agent watches a directory (polling by default), processes new documents,
splits them into chunks, computes deterministic IDs, embeds them using a pluggable
embedder, and stores vectors+metadata into ChromaDB.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set

from chromadb.api.models.Collection import Collection

from bamboo_mcp_services.agents.base import Agent
from bamboo_mcp_services.common.fs import is_hidden_name
from .utils import (
    extract_text_from_file,
    chunk_text,
    content_hash,
    deterministic_chunk_id,
    summarise_chunks,
    CheckpointStore,
)
from .storage import ChromaWrapper, CollectionRouter, FileVersions

LOG = logging.getLogger(__name__)


@dataclass
class _CyclePlan:
    """What one poll cycle found on disk.

    Attributes:
        present: Files that yielded text, mapped to their content hash.
        candidates: New or changed files, mapped to their text.
        unreadable: Files whose text could not be read this cycle.
        removed: Checkpointed files that are gone or no longer yield text.
    """

    present: Dict[str, str] = field(default_factory=dict)
    candidates: Dict[str, str] = field(default_factory=dict)
    unreadable: Set[str] = field(default_factory=set)
    removed: List[str] = field(default_factory=list)


@dataclass
class _CycleResult:
    """What one poll cycle wrote into the idle slot.

    Attributes:
        expected: File versions the idle slot must hold, with record counts.
        updates: Checkpoint snapshots for files embedded this cycle.
        ingested: Files embedded successfully.
        repaired: Of those, files re-embedded because the live slot lacked them.
        carried: Files carried over from the live slot.
        failed: Files whose embedding failed.
    """

    expected: FileVersions = field(default_factory=dict)
    updates: Dict[str, Dict] = field(default_factory=dict)
    ingested: int = 0
    repaired: int = 0
    carried: int = 0
    failed: int = 0


def compare_versions(expected: FileVersions, actual: FileVersions) -> List[str]:
    """List the differences between the file versions a slot should and does hold.

    Args:
        expected: ``{source_file: {content_hash: record count}}`` written this cycle.
        actual: The same, as read back from the slot.

    Returns:
        One human-readable line per differing file; empty when they match.
    """
    problems: List[str] = []
    for path_str in sorted(expected.keys() | actual.keys()):
        want = expected.get(path_str)
        got = actual.get(path_str)
        if want == got:
            continue
        if got is None:
            problems.append(f"missing {path_str}")
        elif want is None:
            problems.append(f"unexpected {path_str}")
        else:
            problems.append(f"{path_str}: expected {want}, found {got}")
    return problems


def list_watched_files(directory: Path) -> List[Path]:
    """Return the files under *directory* that document-monitor should consider.

    Dot-files are skipped: they include the temporary files github-sync writes
    and then renames into place (and any left behind by a sync killed
    mid-write), plus clutter such as ``.DS_Store``.  Any other file is read,
    whatever its extension, so a temporary file would otherwise be embedded.
    Only the file's own name is checked, so documents under a dot-directory
    such as ``.github/`` are still indexed.

    Args:
        directory: Watched directory.

    Returns:
        Matching files, sorted.
    """
    return sorted(p for p in directory.rglob("*") if p.is_file() and not is_hidden_name(p))


class DocumentMonitorAgent(Agent):
    """Agent that monitors a directory and ingests new files into ChromaDB.

    The agent lifecycle integrates with the project's Base Agent: it must
    implement start/tick/stop hooks via the base class (the names used here
    match a thin adapter to your existing base).

    Args:
        name: Agent name.
        directory: Directory to monitor (create if missing).
        poll_interval_sec: Polling interval in seconds.
        chunk_size: Character chunk size (default: 3000).
        chunk_overlap: Chunk overlap in characters (default: 300).
        checkpoint_file: Path to JSON checkpoint file.
        chroma_dir: Directory for ChromaDB persistence.
        embedder: Object with an .encode(list[str], show_progress_bar=False) -> np.ndarray interface.
                  If None, a default local sentence-transformers embedder will be created lazily.
    """

    def __init__(
        self,
        name: str,
        directory: str,
        poll_interval_sec: int = 10,
        chunk_size: int = 3000,
        chunk_overlap: int = 300,
        checkpoint_file: str = ".document_monitor/checkpoints.json",
        chroma_dir: str = ".chromadb",
        embedder: Optional[object] = None,
        embedding_model_name: str = "all-MiniLM-L6-v2",
    ) -> None:
        super().__init__(name=name)
        self.directory = Path(directory)
        self.poll_interval_sec = poll_interval_sec
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.checkpoint = CheckpointStore(checkpoint_file)
        self.chroma = ChromaWrapper(persist_directory=chroma_dir)

        # The router maps the logical collection name (== agent name) to one of
        # two physical ChromaDB slot names (<name>__a or <name>__b).  Readers
        # always address the live slot; writes go to the idle slot, which is
        # then promoted atomically via commit_swap.
        _sidecar = str(Path(chroma_dir) / "collection_routing.json")
        self.router = CollectionRouter(sidecar_path=_sidecar)
        self._logical_name = name

        # Resolve (or initialise) the live physical collection on startup.
        live_physical = self.router.live_name(name)
        self.collection = self.chroma.get_or_create_collection(live_physical)
        LOG.info(
            "DocumentMonitorAgent: logical='%s' live_slot='%s'",
            name, live_physical,
        )

        self._last_processed_file: Optional[str] = None
        self._last_error: Optional[str] = None
        self._embedder = embedder
        self._embedding_model_name = embedding_model_name

    # ---------------------- embedder ---------------------------------------
    def _ensure_embedder(self) -> None:
        """Ensure an embedder is available; instantiate default if not provided."""
        if self._embedder is not None:
            return
        try:
            # Lazy import to avoid hard requirement in test/mocked environments
            from sentence_transformers import SentenceTransformer  # type: ignore

            self._embedder = SentenceTransformer(self._embedding_model_name)
        except Exception as exc:
            LOG.exception("Failed to create default embedder: %s", exc)
            raise

    # ---------------------- lifecycle hooks --------------------------------
    def _start_impl(self) -> None:
        """Start hook called by base Agent.start().

        Creates the monitored directory if missing and performs any one-time init.
        """
        try:
            from importlib.metadata import version
            _version = version("bamboo-mcp-services")
        except Exception:
            _version = "unknown"
        LOG.info("document-monitor-agent v%s starting. Monitoring: %s", _version, self.directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def _is_file_changed(
        self,
        path_str: str,
        text: str,
        file_num: int = 0,
        total: int = 0,
    ) -> tuple[bool, str, list]:
        """Check whether a file needs ingesting by comparing its content hash to the checkpoint.

        Args:
            path_str: Absolute path of the file under inspection.
            text: Full extracted text of the file.
            file_num: 1-based index of this file in the current scan (used in
                log output when *total* is also provided).
            total: Total number of files found in the current scan pass.

        Returns:
            Tuple of (changed, content_hash, prev_chunk_ids).
        """
        h = content_hash(text)
        prev = self.checkpoint._data.get("processed", {}).get(path_str)
        prev_hash = prev.get("content_hash") if prev else None
        prev_chunk_ids = prev.get("chunk_ids", []) if prev else []

        if prev_hash == h:
            return False, h, prev_chunk_ids

        if prev_hash is None:
            if file_num and total:
                LOG.info("New file detected (#%d/%d): %s", file_num, total, path_str)
            else:
                LOG.info("New file detected: %s", path_str)
        else:
            LOG.info("File changed, re-ingesting: %s", path_str)

        return True, h, prev_chunk_ids

    def _processed(self) -> Dict[str, Dict]:
        """Return the checkpoint's ``processed`` mapping (filename -> snapshot).

        Returns:
            The live mapping held by the checkpoint store.
        """
        return self.checkpoint._data.get("processed", {})

    def _ingest_file(
        self,
        path_str: str,
        text: str,
        h: str,
        target_col: object,
    ) -> Dict:
        """Chunk, embed, and write a single file's vectors into *target_col*.

        This method is a **pure writer** — it has no knowledge of blue/green
        slots, routing or the checkpoint.  It returns the checkpoint snapshot
        for the file; :meth:`_tick_impl` records it only after the slot it was
        written into has gone live, so the checkpoint never claims a version
        readers cannot see.

        Args:
            path_str: Absolute path to the file being ingested.
            text: Full extracted text of the file.
            h: SHA-256 content hash of *text*.
            target_col: Open ChromaDB Collection object to write into.

        Returns:
            Checkpoint snapshot: ``content_hash``, ``processed_ts``, ``chunks``
            and ``chunk_ids``.
        """
        chunks = chunk_text(text, chunk_size=self.chunk_size, overlap=self.chunk_overlap)
        ts = datetime.now(timezone.utc).isoformat()

        if not chunks:
            LOG.debug("No chunks generated for %s; recording empty checkpoint.", path_str)
            self._last_processed_file = path_str
            self._last_error = None
            return {"content_hash": h, "processed_ts": ts, "chunks": 0, "chunk_ids": []}

        # For PDF files, log a chunk-quality summary so operators can verify
        # that text extraction produced sensible content.  Scanned PDFs that
        # slipped through the empty-text guard (e.g. a PDF with a tiny stub
        # text layer) would show very short chunk lengths here.
        if Path(path_str).suffix.lower() == ".pdf":
            summary = summarise_chunks(path_str, chunks)
            if summary:
                LOG.info("%s", summary)

        ids: List[str] = [deterministic_chunk_id(path_str, "", i) for i in range(len(chunks))]
        metadatas: List[Dict] = [
            {"source_file": path_str, "chunk_index": i, "content_hash": h, "processed_ts": ts}
            for i in range(len(chunks))
        ]

        self._ensure_embedder()
        raw_embeddings = self._embedder.encode(chunks, show_progress_bar=False)
        try:
            embeddings = raw_embeddings.tolist()  # type: ignore[attr-defined]
        except Exception:
            embeddings = [list(map(float, v)) for v in raw_embeddings]

        self.chroma.add_documents(
            target_col,
            ids=ids,
            documents=chunks,
            metadatas=metadatas,
            embeddings=embeddings,
        )

        self._last_processed_file = path_str
        self._last_error = None
        LOG.info("Processed file %s -> chunks=%d", path_str, len(chunks))
        return {"content_hash": h, "processed_ts": ts, "chunks": len(chunks), "chunk_ids": ids}

    def _plan_cycle(self, files: List[Path]) -> _CyclePlan:
        """Read every watched file and decide what this cycle has to do.

        Args:
            files: Files returned by :func:`list_watched_files`.

        Returns:
            The cycle plan.  A file whose text cannot be read is listed as
            unreadable rather than removed, so a transient read error never
            drops a document from the store.
        """
        plan = _CyclePlan()
        total = len(files)
        for file_num, p in enumerate(files, start=1):
            path_str = str(p.resolve())
            try:
                text = extract_text_from_file(path_str)
            except Exception as exc:
                LOG.exception("Error reading file %s: %s", path_str, exc)
                self._last_error = str(exc)
                plan.unreadable.add(path_str)
                continue
            if not text:
                LOG.debug("No text extracted from %s; skipping.", path_str)
                continue
            changed, h, _ = self._is_file_changed(path_str, text, file_num, total)
            plan.present[path_str] = h
            if changed:
                plan.candidates[path_str] = text

        plan.removed = [p for p in self._processed() if p not in plan.present and p not in plan.unreadable]
        for path_str in plan.removed:
            LOG.info("File removed or no longer yields text, dropping: %s", path_str)
        return plan

    def _plan_carry(self, plan: _CyclePlan, live: FileVersions) -> tuple[Dict[str, str], Dict[str, str]]:
        """Split the files this cycle does not re-embed into carried and repaired.

        An unchanged file is carried over from the live slot when the live slot
        holds the version the checkpoint records.  If it does not — a store
        left inconsistent by an earlier version of this agent — the file is
        re-embedded instead, so the new slot is complete.

        Args:
            plan: The cycle plan.
            live: Versions held by the live slot.

        Returns:
            ``(carry, repair)``: files to carry as ``{path: content_hash}``, and
            files to re-embed as ``{path: text}``.
        """
        processed = self._processed()
        carry: Dict[str, str] = {}
        repair: Dict[str, str] = {}
        keep = [p for p in plan.present if p not in plan.candidates] + sorted(plan.unreadable)
        for path_str in keep:
            meta = processed.get(path_str) or {}
            h = meta.get("content_hash")
            if not meta.get("chunks") or not h:
                continue
            if live.get(path_str, {}).get(h):
                carry[path_str] = h
            elif path_str in plan.unreadable:
                LOG.warning("Cannot carry or re-read %s; it will be missing until it is readable.", path_str)
            else:
                try:
                    repair[path_str] = extract_text_from_file(path_str)
                except Exception as exc:
                    LOG.exception("Error re-reading %s for repair: %s", path_str, exc)
                    self._last_error = str(exc)
        if repair:
            LOG.warning(
                "Live slot is missing %d checkpointed file(s); re-embedding them.", len(repair),
            )
        return carry, repair

    def _build_idle_slot(self, plan: _CyclePlan, idle_col: Collection) -> _CycleResult:
        """Write the complete corpus for this cycle into the idle slot.

        Changed, new and repaired files are embedded.  Every other file is
        carried over from the live slot by copying its stored records, so the
        idle slot holds the whole corpus, not just what changed.  A changed
        file whose embedding fails keeps its previous version.

        Args:
            plan: The cycle plan.
            idle_col: The freshly recreated idle collection.

        Returns:
            What was written and what the slot is expected to contain.
        """
        result = _CycleResult()
        processed = self._processed()
        live = self.chroma.file_versions(self.collection)
        carry, repair = self._plan_carry(plan, live)
        result.repaired = len(repair)

        for path_str, text in {**plan.candidates, **repair}.items():
            h = plan.present[path_str]
            try:
                snapshot = self._ingest_file(path_str, text, h, idle_col)
            except Exception as exc:
                LOG.exception("Error processing file %s: %s", path_str, exc)
                self._last_error = str(exc)
                result.failed += 1
                old = processed.get(path_str) or {}
                old_hash = old.get("content_hash")
                if old.get("chunks") and old_hash and live.get(path_str, {}).get(old_hash):
                    carry[path_str] = old_hash
                continue
            result.ingested += 1
            result.updates[path_str] = snapshot
            if snapshot["chunks"]:
                result.expected[path_str] = {h: snapshot["chunks"]}

        copied = self.chroma.copy_files(self.collection, idle_col, carry)
        result.carried = len(copied)
        for path_str, h in carry.items():
            result.expected[path_str] = {h: live[path_str][h]}
        return result

    def _promote_idle_slot(self, plan: _CyclePlan, idle_physical: str, idle_col: Collection, result: _CycleResult) -> None:
        """Promote the idle slot if it is complete, then commit the checkpoint.

        The slot is discarded, and the live slot left serving, when nothing
        was written successfully, when it would hold no documents, or when its
        contents differ from what this cycle wrote into it.  The checkpoint is
        updated only after a successful swap.

        Args:
            plan: The cycle plan.
            idle_physical: Name of the idle collection.
            idle_col: The idle collection.
            result: What :meth:`_build_idle_slot` wrote.
        """
        if result.ingested == 0 and not plan.removed:
            LOG.warning(
                "Poll cycle: all %d file(s) to embed failed; idle slot cleaned up, no swap performed.",
                result.failed,
            )
            self.chroma.delete_collection(idle_physical)
            return
        if not result.expected:
            LOG.warning("Poll cycle would leave '%s' with no documents; keeping the live slot.", self._logical_name)
            self.chroma.delete_collection(idle_physical)
            return
        problems = compare_versions(result.expected, self.chroma.file_versions(idle_col))
        if problems:
            message = (
                f"Idle slot '{idle_physical}' does not hold the expected corpus "
                f"({len(problems)} file(s) differ); not promoted, live slot unchanged: "
                + "; ".join(problems[:5])
            )
            LOG.error("%s", message)
            self._last_error = message
            self.chroma.delete_collection(idle_physical)
            return

        self.router.commit_swap(self._logical_name, self.chroma)
        new_live = self.router.live_name(self._logical_name)
        self.collection = self.chroma.get_or_create_collection(new_live)
        self.chroma.persist()
        self.checkpoint.apply(result.updates, plan.removed)

        # Post-cycle invariant check: verify all routed collections are non-empty.
        try:
            self.router.verify_routing_invariant(self.chroma)
        except RuntimeError as inv_exc:
            LOG.error("Post-cycle routing invariant FAILED: %s", inv_exc)
            self._last_error = str(inv_exc)

        LOG.info(
            "Poll cycle complete: %d file(s) embedded (%d repaired), %d carried over, "
            "%d removed, %d failed. Live slot: %s. Next poll in %ds.",
            result.ingested, result.repaired, result.carried, len(plan.removed),
            result.failed, new_live, self.poll_interval_sec,
        )

    def _tick_impl(self) -> None:
        """Perform one polling cycle: detect changes and rebuild the idle slot.

        Blue/green slot lifecycle
        -------------------------
        1. Read every watched file.  If no file is new, changed or removed,
           skip the cycle without touching ChromaDB.
        2. Delete and recreate the idle slot.  Recreating from scratch clears
           any embedding dimension inherited from a previous cycle.
        3. Embed new and changed files into the idle slot, and carry every
           unchanged file over from the live slot by copying its stored
           records.  Removed files are not carried.  A changed file whose
           embedding fails keeps its previous version.
        4. Check that the idle slot holds exactly the expected file versions;
           if not, discard it and leave the live slot serving.
        5. Promote the idle slot with
           :meth:`~storage.CollectionRouter.commit_swap`, then record the
           cycle in the checkpoint.

        The idle slot therefore always holds the whole corpus, never just the
        files that changed, and the checkpoint is never ahead of the live slot.
        """
        try:
            files = list_watched_files(self.directory)
        except Exception as exc:
            LOG.exception("Failed listing directory %s: %s", self.directory, exc)
            self._last_error = str(exc)
            time.sleep(self.poll_interval_sec)
            return

        plan = self._plan_cycle(files)
        if not plan.candidates and not plan.removed:
            LOG.debug(
                "Poll cycle complete: no changes detected (%d file(s) unchanged). "
                "Next poll in %ds.", len(plan.present), self.poll_interval_sec,
            )
            time.sleep(self.poll_interval_sec)
            return

        idle_physical = self.router.idle_name(self._logical_name)
        self.chroma.delete_collection(idle_physical)
        idle_col = self.chroma.create_collection(idle_physical)
        try:
            result = self._build_idle_slot(plan, idle_col)
            self._promote_idle_slot(plan, idle_physical, idle_col, result)
        except Exception as exc:
            # Unexpected error outside the per-file loop — clean up and bail.
            LOG.exception("Unexpected error during poll cycle: %s", exc)
            self._last_error = str(exc)
            self.chroma.delete_collection(idle_physical)

        time.sleep(self.poll_interval_sec)

    def _stop_impl(self) -> None:
        """Stop hook called by base Agent.stop().

        Persist chroma and perform cleanup.
        """
        LOG.info("document_monitor_agent stopping. Persisting Chroma.")
        try:
            self.chroma.persist()
        except Exception:
            LOG.exception("Failed persisting chroma on stop")

    def _health_details(self) -> Dict:
        """Return agent-specific health details for monitoring dashboards.

        Returns:
            Dictionary with last processed file, last error, checkpoint location,
            logical collection name, and the active physical ChromaDB slot.
        """
        live_slot = self.router.live_name(self._logical_name)
        return {
            "last_processed_file": self._last_processed_file,
            "last_error": self._last_error,
            "checkpoint_file": str(self.checkpoint.path),
            "chroma_collection": self._logical_name,
            "chroma_live_slot": live_slot,
        }
