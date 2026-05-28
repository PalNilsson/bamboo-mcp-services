"""Document monitor agent implementation.

This agent watches a directory (polling by default), processes new documents,
splits them into chunks, computes deterministic IDs, embeds them using a pluggable
embedder, and stores vectors+metadata into ChromaDB.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict

from bamboo_mcp_services.agents.base import Agent
from .utils import (
    extract_text_from_file,
    chunk_text,
    content_hash,
    deterministic_chunk_id,
    CheckpointStore,
)
from .storage import ChromaWrapper, CollectionRouter

LOG = logging.getLogger(__name__)


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

    def _is_file_changed(self, path_str: str, text: str) -> tuple[bool, str, list]:
        """Check whether a file needs ingesting by comparing its content hash to the checkpoint.

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
            LOG.info("New file detected: %s", path_str)
        else:
            LOG.info("File changed, re-ingesting: %s", path_str)

        return True, h, prev_chunk_ids

    def _ingest_file(
        self,
        path_str: str,
        text: str,
        h: str,
        target_col: object,
    ) -> None:
        """Chunk, embed, and write a single file's vectors into *target_col*.

        This method is a **pure writer** — it has no knowledge of blue/green
        slots or routing.  Slot lifecycle (idle slot creation, promotion, and
        cleanup on failure) is managed entirely by :meth:`_tick_impl`, which
        calls this method for every changed file in a cycle and passes the same
        idle collection object each time.  This ensures the idle slot
        accumulates all changed files before being promoted as a complete corpus.

        Args:
            path_str: Absolute path to the file being ingested.
            text: Full extracted text of the file.
            h: SHA-256 content hash of *text*.
            target_col: Open ChromaDB Collection object to write into.
        """
        chunks = chunk_text(text, chunk_size=self.chunk_size, overlap=self.chunk_overlap)
        ts = datetime.now(timezone.utc).isoformat()

        if not chunks:
            LOG.debug("No chunks generated for %s; recording empty checkpoint.", path_str)
            self.checkpoint.mark_processed(
                path_str,
                {"content_hash": h, "processed_ts": ts, "chunks": 0, "chunk_ids": []},
            )
            self._last_processed_file = path_str
            self._last_error = None
            return

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

        self.checkpoint.mark_processed(
            path_str,
            {"content_hash": h, "processed_ts": ts, "chunks": len(chunks), "chunk_ids": ids},
        )

        self._last_processed_file = path_str
        self._last_error = None
        LOG.info("Processed file %s -> chunks=%d", path_str, len(chunks))

    def _tick_impl(self) -> None:
        """Perform one polling cycle: detect new/changed files, ingest into ChromaDB.

        Blue/green slot lifecycle
        -------------------------
        The idle slot is managed at the **cycle** level, not the file level:

        1. Scan all files; if none have changed skip slot management entirely.
        2. Delete and recreate the idle slot once at the start of the cycle.
           Recreating from scratch clears any embedding dimension inherited from
           a previous cycle, guarding against dimension-mismatch errors when the
           embedder model changes.
        3. Write every changed file's chunks into the **same** idle collection.
           All files accumulate into one slot before any swap occurs.
        4. After all files are written, call :meth:`~storage.CollectionRouter.commit_swap`
           once.  The routing sidecar is updated atomically (``os.replace``) and
           readers are transparently redirected to the new slot.
        5. If any file write fails the error is logged and the file is skipped,
           but the cycle continues.  The slot is still promoted at the end with
           whatever was successfully written — a partial update is better than no
           update.  If *no* files were written successfully the idle slot is
           cleaned up and no swap occurs, leaving the live slot untouched.

        Errors are caught per-file so one bad file does not abort the whole cycle.
        """
        try:
            files = sorted([p for p in self.directory.rglob("*") if p.is_file()])
        except Exception as exc:
            LOG.exception("Failed listing directory %s: %s", self.directory, exc)
            self._last_error = str(exc)
            time.sleep(self.poll_interval_sec)
            return

        # --- First pass: identify which files need ingesting ---------------
        candidates: list[tuple[str, str, str]] = []  # (path_str, text, hash)
        skipped_count = 0

        for p in files:
            path_str = str(p.resolve())
            try:
                text = extract_text_from_file(path_str)
                if not text:
                    LOG.debug("No text extracted from %s; skipping.", path_str)
                    continue
                changed, h, _ = self._is_file_changed(path_str, text)
                if not changed:
                    skipped_count += 1
                    continue
                candidates.append((path_str, text, h))
            except Exception as exc:
                LOG.exception("Error reading file %s: %s", path_str, exc)
                self._last_error = str(exc)

        if not candidates:
            LOG.debug(
                "Poll cycle complete: no changes detected (%d file(s) unchanged). "
                "Next poll in %ds.", skipped_count, self.poll_interval_sec,
            )
            time.sleep(self.poll_interval_sec)
            return

        # --- Open the idle slot once for the whole cycle -------------------
        idle_physical = self.router.idle_name(self._logical_name)

        # Always delete then recreate — clears any locked embedding dimension
        # from a previous cycle so a changed embedder model never causes a
        # dimension-mismatch error.
        self.chroma.delete_collection(idle_physical)
        idle_col = self.chroma.create_collection(idle_physical)

        # --- Second pass: write all candidates into the idle slot ----------
        processed_count = 0

        try:
            for path_str, text, h in candidates:
                try:
                    self._ingest_file(path_str, text, h, idle_col)
                    processed_count += 1
                except Exception as exc:
                    LOG.exception("Error processing file %s: %s", path_str, exc)
                    self._last_error = str(exc)

            if processed_count == 0:
                # Every file failed — clean up the idle slot and leave the live
                # slot untouched.
                LOG.warning(
                    "Poll cycle: all %d candidate(s) failed; idle slot cleaned up, "
                    "no swap performed.", len(candidates),
                )
                self.chroma.delete_collection(idle_physical)
            else:
                # Atomically promote the idle slot.  Updates the routing sidecar
                # (os.replace) and deletes the old live collection.
                self.router.commit_swap(self._logical_name, self.chroma)
                new_live = self.router.live_name(self._logical_name)
                self.collection = self.chroma.get_or_create_collection(new_live)
                self.chroma.persist()
                LOG.info(
                    "Poll cycle complete: %d file(s) ingested, %d unchanged. "
                    "Live slot: %s. Next poll in %ds.",
                    processed_count, skipped_count, new_live, self.poll_interval_sec,
                )

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
