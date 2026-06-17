"""CLI entrypoint for document_monitor_agent."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import warnings
from collections.abc import Callable
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Optional

from .agent import DocumentMonitorAgent
from .embedder_langchain_hf import DummyEmbedder, LangchainHuggingFaceAdapter
from bamboo_mcp_services.common.cli import log_startup_banner

logger = logging.getLogger(__name__)


class _SuppressNameAtInfo(logging.Filter):
    """Blank the ``name`` field for INFO-level records.

    At INFO verbosity the logger hierarchy (e.g.
    ``bamboo_mcp_services.agents.document_monitor_agent.agent``) adds noise
    without value — almost every line comes from the same module.  WARNING and
    above keep the full name so that unexpected sources are still identifiable.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        if record.levelno == logging.INFO:
            record.name = ""
        return True


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser for the document monitor agent.

    Returns:
        argparse.ArgumentParser: Configured parser ready to call
            ``parse_args()`` on.
    """
    p = argparse.ArgumentParser(prog="bamboo-document-monitor")

    # ── Multi-watch (current API) ─────────────────────────────────────────
    p.add_argument(
        "--watch",
        nargs=2,
        metavar=("DIR", "COLLECTION"),
        action="append",
        dest="watches",
        default=None,
        help=(
            "Directory to monitor and the ChromaDB collection to ingest into. "
            "Repeat to watch multiple directories, e.g.: "
            "--watch ./data/panda_docs panda_docs "
            "--watch ./data/bamboo_docs bamboo_docs"
        ),
    )

    # ── Legacy single-dir flags (deprecated, kept for backward compat) ────
    p.add_argument(
        "--dir", "-d",
        dest="legacy_dir",
        default=None,
        help=(
            "DEPRECATED: use --watch DIR COLLECTION instead. "
            "Directory to monitor (e.g. ./documents)."
        ),
    )
    p.add_argument(
        "--collection",
        dest="legacy_collection",
        default="atlas_docs",
        help=(
            "DEPRECATED: use --watch DIR COLLECTION instead. "
            "ChromaDB collection name (default: atlas_docs)."
        ),
    )

    # ── Shared flags ──────────────────────────────────────────────────────
    p.add_argument("--poll-interval", type=int, default=10, help="Poll interval seconds")
    p.add_argument("--chroma-dir", default=".chromadb", help="ChromaDB persist directory")
    p.add_argument(
        "--checkpoint-dir",
        default=".document_monitor",
        help="Directory for per-watch checkpoint files (default: .document_monitor).",
    )
    p.add_argument("--chunk-size", type=int, default=3000, help="Chunk size in characters")
    p.add_argument("--chunk-overlap", type=int, default=300, help="Chunk overlap in characters")
    p.add_argument(
        "--model-path",
        default=None,
        metavar="PATH",
        help=(
            "Absolute path to the local sentence-transformers model directory "
            "(e.g. /data/models/all-MiniLM-L6-v2).  "
            "When set, the embedder loads the model from this path and treats "
            "any load failure as fatal — the agent will exit rather than "
            "silently falling back to a DummyEmbedder.  "
            "When omitted, the default model name ('all-MiniLM-L6-v2') is "
            "resolved via the HuggingFace cache; if that also fails the "
            "DummyEmbedder fallback is used (development / CI only)."
        ),
    )
    p.add_argument(
        "--once",
        action="store_true",
        help="Run a single poll cycle per watch pair then exit.",
    )
    p.add_argument(
        "--log-file",
        default=None,
        metavar="PATH",
        help=(
            "Append log output to PATH in addition to stderr.  "
            "The file and any missing parent directories are created "
            "automatically.  Uses a rotating file handler capped at "
            "10 MB per file with up to 5 backups."
        ),
    )
    return p


def _resolve_watches(args: argparse.Namespace) -> list[tuple[str, str]]:
    """Return a list of (directory, collection) pairs from parsed arguments.

    Merges ``--watch`` pairs and the legacy ``--dir``/``--collection`` flags.
    Emits a deprecation warning when the legacy flags are used.

    Args:
        args: Parsed argument namespace from :func:`build_parser`.

    Returns:
        Non-empty list of ``(dir_path, collection_name)`` tuples.

    Raises:
        SystemExit: If no watch pair can be resolved.
    """
    watches: list[tuple[str, str]] = []

    if args.watches:
        watches.extend((d, c) for d, c in args.watches)

    if args.legacy_dir is not None:
        warnings.warn(
            "--dir/--collection are deprecated; use --watch DIR COLLECTION instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        watches.append((args.legacy_dir, args.legacy_collection))

    if not watches:
        logger.error(
            "No directories to watch. "
            "Specify at least one --watch DIR COLLECTION pair."
        )
        sys.exit(1)

    return watches


def _checkpoint_path(checkpoint_dir: str, directory: str, collection: str) -> str:
    """Derive a per-watch-pair checkpoint filename.

    Uses the last component of *directory* combined with *collection* to
    produce a deterministic, human-readable filename that is unique for each
    (directory, collection) pair even when two pairs share the same collection
    name.

    Example::

        _checkpoint_path(".document_monitor", "/data/bamboo/rag/panda_docs", "panda_docs")
        # → ".document_monitor/checkpoints_panda_docs_panda_docs.json"

    Args:
        checkpoint_dir: Root directory for checkpoint files.
        directory: Watched filesystem path.
        collection: Logical ChromaDB collection name.

    Returns:
        Absolute-or-relative path string for the checkpoint JSON file.
    """
    dir_tag = Path(directory).name.replace(" ", "_")
    return str(Path(checkpoint_dir) / f"checkpoints_{dir_tag}_{collection}.json")


def _build_embedder(model_path: str | None = None) -> LangchainHuggingFaceAdapter:
    """Instantiate the HuggingFace sentence-embedding model.

    When *model_path* is provided the adapter is told to load from that exact
    local directory.  Any failure is re-raised as a :class:`RuntimeError` so
    the process exits loudly rather than silently continuing with a
    :class:`DummyEmbedder` that writes zero-vector garbage into ChromaDB.

    When *model_path* is ``None`` the adapter falls back to the default
    model-name lookup (HuggingFace cache / network), which may degrade to
    :class:`DummyEmbedder` in offline environments — acceptable for
    development and CI only.

    Args:
        model_path: Absolute path to a locally cached sentence-transformers
            model directory, or ``None`` to use the default name lookup.

    Returns:
        LangchainHuggingFaceAdapter: Ready-to-use embedding adapter.

    Raises:
        RuntimeError: If *model_path* is given but the model cannot be loaded.
    """
    name = model_path if model_path is not None else "all-MiniLM-L6-v2"
    adapter = LangchainHuggingFaceAdapter(model_name=name)
    if model_path is not None and isinstance(adapter._embedder, DummyEmbedder):
        raise RuntimeError(
            f"--model-path '{model_path}' was specified but the model could not "
            "be loaded — refusing to start with DummyEmbedder.  "
            "Verify the path points to a valid sentence-transformers model directory."
        )
    return adapter


def _configure_logging(log_file: str | None, suppress_filter: logging.Filter) -> None:
    """Attach a rotating file handler to the root logger if *log_file* is set.

    The handler mirrors the format used by the stream handler configured in
    :func:`main` so that console and file output are identical.
    :class:`_SuppressNameAtInfo` is applied to both handlers so INFO-level
    records omit the logger hierarchy in both destinations.

    The file and any missing parent directories are created automatically.
    The handler rotates at 10 MB per file and retains up to 5 backups.

    Args:
        log_file: Filesystem path for the log file, or ``None`` to skip.
        suppress_filter: Pre-constructed :class:`_SuppressNameAtInfo` instance
            to attach to the new handler.
    """
    if log_file is None:
        return

    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    handler = RotatingFileHandler(
        log_path,
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s%(message)s")
    )
    handler.addFilter(suppress_filter)
    logging.getLogger().addHandler(handler)
    logger.info("Log file: %s", log_path)


def _build_agents(args: argparse.Namespace) -> list[DocumentMonitorAgent]:
    """Construct one :class:`DocumentMonitorAgent` per watch pair.

    The embedder instance is shared across all agents to avoid loading the
    sentence-transformer model multiple times.

    Args:
        args: Namespace produced by :func:`build_parser` after calling
            ``parse_args()``.

    Returns:
        List of fully configured :class:`DocumentMonitorAgent` instances,
        one per ``(directory, collection)`` watch pair.

    Raises:
        RuntimeError: If ``--model-path`` is specified but the model fails to
            load (propagated from :func:`_build_embedder`).
    """
    watches = _resolve_watches(args)
    embedder = _build_embedder(model_path=getattr(args, "model_path", None))
    agents = []
    for directory, collection in watches:
        cp_file = _checkpoint_path(args.checkpoint_dir, directory, collection)
        agent = DocumentMonitorAgent(
            name=collection,
            directory=directory,
            poll_interval_sec=args.poll_interval,
            chunk_size=args.chunk_size,
            chunk_overlap=args.chunk_overlap,
            checkpoint_file=cp_file,
            chroma_dir=args.chroma_dir,
            embedder=embedder,
        )
        agents.append(agent)
    return agents


def _make_signal_handler(
    agents: list[DocumentMonitorAgent],
) -> Callable[[int, Any], None]:
    """Create a POSIX signal handler that gracefully stops all *agents*.

    Args:
        agents: Running agent instances to shut down when a signal is received.

    Returns:
        Callable[[int, Any], None]: A signal handler with the standard
            ``(signum, frame)`` signature.
    """

    def _handler(_signum: int, _frame: Any) -> None:
        logger.info("Signal received; attempting graceful shutdown.")
        for agent in agents:
            try:
                if hasattr(agent, "stop"):
                    agent.stop()
                else:
                    logger.warning("Agent %s has no stop method.", agent.name)
            except Exception:
                logger.exception("Error while stopping agent %s.", agent.name)

    return _handler


def _agent_is_running(obj: Any) -> bool:
    """Return ``True`` if *obj* appears to be in a ``RUNNING`` state."""
    state = getattr(obj, "state", None)
    if state is None:
        return False
    name = getattr(state, "name", None)
    if isinstance(name, str):
        return name.upper() == "RUNNING"
    if hasattr(obj, "RUNNING"):
        try:
            if state == getattr(obj, "RUNNING"):
                return True
        except Exception:
            pass
    try:
        return str(state).upper().endswith("RUNNING") or str(state).upper() == "RUNNING"
    except Exception:
        return False


def _run_agents(agents: list[DocumentMonitorAgent], once: bool = False) -> None:
    """Start all agents and run ticks sequentially until stopped.

    In ``--once`` mode every agent executes a single tick in order, then all
    are stopped.  In daemon mode the loop iterates over agents in round-robin
    order for as long as every agent is running.

    Agents are run sequentially (not in threads) to keep the implementation
    simple and to avoid locking complexity between agents that share the same
    ``--chroma-dir``.

    Args:
        agents: Fully configured agent instances.
        once: If ``True``, run exactly one tick per agent then return.
    """
    for agent in agents:
        agent.start()
    try:
        if once:
            logger.info(
                "--once flag set: running a single poll cycle per watch pair then exiting."
            )
            for agent in agents:
                agent.tick()
            return
        while all(_agent_is_running(a) for a in agents):
            for agent in agents:
                if _agent_is_running(agent):
                    agent.tick()
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received; shutting down.")
        for agent in agents:
            try:
                agent.stop()
            except Exception:
                pass
        return
    finally:
        for agent in agents:
            try:
                agent.stop()
            except Exception:
                pass


def main(argv: Optional[list[str]] = None) -> None:
    """Run the document monitor agent from the command line.

    Args:
        argv: Argument list to parse.  When ``None`` (the default),
            :data:`sys.argv` ``[1:]`` is used automatically by
            :mod:`argparse`.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s%(message)s",
    )
    _filter = _SuppressNameAtInfo()
    for _h in logging.root.handlers:
        _h.addFilter(_filter)
    _configure_logging(getattr(args, "log_file", None), _filter)
    log_startup_banner(logger, "bamboo-document-monitor")

    for _noisy in ("httpx", "httpcore", "huggingface_hub", "sentence_transformers"):
        logging.getLogger(_noisy).setLevel(logging.WARNING)

    _hf_hub_cache = os.path.expanduser("~/.cache/huggingface/hub")
    os.environ.setdefault("SENTENCE_TRANSFORMERS_HOME", _hf_hub_cache)
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    os.environ.setdefault("HF_HUB_OFFLINE", "1")

    agents = _build_agents(args)
    signal.signal(signal.SIGTERM, _make_signal_handler(agents))
    _run_agents(agents, once=args.once)


if __name__ == "__main__":
    main(sys.argv[1:])
