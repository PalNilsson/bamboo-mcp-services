"""CLI entrypoint for document_monitor_agent."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
from collections.abc import Callable
from typing import Any, Optional

from .agent import DocumentMonitorAgent
from .embedder_langchain_hf import LangchainHuggingFaceAdapter
from bamboo_mcp_services.common.cli import log_startup_banner

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser for the document monitor agent.

    Defines all supported command-line flags with their types, defaults,
    and help strings.

    Returns:
        argparse.ArgumentParser: Configured parser ready to call
            ``parse_args()`` on.
    """
    p = argparse.ArgumentParser(prog="bamboo-document-monitor")
    p.add_argument("--dir", "-d", required=True, help="Directory to monitor (e.g. ./documents)")
    p.add_argument("--poll-interval", type=int, default=10, help="Poll interval seconds")
    p.add_argument("--chroma-dir", default=".chromadb", help="ChromaDB persist directory")
    p.add_argument(
        "--collection",
        default="atlas_docs",
        help="ChromaDB collection name (default: atlas_docs).",
    )
    p.add_argument(
        "--checkpoint-file",
        default=".document_monitor/checkpoints.json",
        help="Checkpoint file path",
    )
    p.add_argument("--chunk-size", type=int, default=3000, help="Chunk size in characters")
    p.add_argument("--chunk-overlap", type=int, default=300, help="Chunk overlap in characters")
    p.add_argument(
        "--once",
        action="store_true",
        help="Run a single poll cycle then exit (useful for cron / one-shot invocations).",
    )
    return p


def _build_embedder() -> LangchainHuggingFaceAdapter:
    """Instantiate the HuggingFace sentence-embedding model.

    Uses the ``all-MiniLM-L6-v2`` model, a compact but accurate
    general-purpose sentence transformer.

    Returns:
        LangchainHuggingFaceAdapter: Ready-to-use embedding adapter.
    """
    return LangchainHuggingFaceAdapter(model_name="all-MiniLM-L6-v2")


def _build_agent(args: argparse.Namespace) -> DocumentMonitorAgent:
    """Construct a :class:`DocumentMonitorAgent` from parsed CLI arguments.

    Args:
        args: Namespace produced by :func:`build_parser` after calling
            ``parse_args()``.  The following attributes are consumed:

            * ``dir`` – directory path to monitor.
            * ``poll_interval`` – polling cadence in seconds.
            * ``chunk_size`` – document chunk size in characters.
            * ``chunk_overlap`` – overlap between consecutive chunks in
              characters.
            * ``checkpoint_file`` – path to the JSON checkpoint file.
            * ``chroma_dir`` – directory used to persist ChromaDB data.
            * ``collection`` – ChromaDB collection name.

    Returns:
        DocumentMonitorAgent: Fully configured agent instance, not yet
            started.
    """
    return DocumentMonitorAgent(
        name=args.collection,
        directory=args.dir,
        poll_interval_sec=args.poll_interval,
        chunk_size=args.chunk_size,
        chunk_overlap=args.chunk_overlap,
        checkpoint_file=args.checkpoint_file,
        chroma_dir=args.chroma_dir,
        embedder=_build_embedder(),
    )


def _make_signal_handler(
    agent: DocumentMonitorAgent,
) -> Callable[[int, Any], None]:
    """Create a POSIX signal handler that gracefully stops *agent*.

    The returned callable is suitable for use with :func:`signal.signal`.
    It attempts ``agent.request_stop()`` first, falls back to
    ``agent.stop()``, and logs a warning if neither method exists.
    All exceptions raised during shutdown are caught and logged so the
    signal handler itself never raises.

    Args:
        agent: The running agent instance to shut down when a signal is
            received.

    Returns:
        Callable[[int, Any], None]: A signal handler with the standard
            ``(signum, frame)`` signature.
    """

    def _handler(_signum: int, _frame: Any) -> None:
        logger.info("Signal received; attempting graceful shutdown.")
        try:
            if hasattr(agent, "stop"):
                agent.stop()
            else:
                logger.warning("Agent has no stop method; nothing to call.")
        except Exception:
            logger.exception("Error while requesting agent to stop.")

    return _handler


def _agent_is_running(obj: Any) -> bool:
    """Return ``True`` if *obj* appears to be in a ``RUNNING`` state.

    The check is intentionally defensive and supports several common
    state representations used across different agent implementations:

    * **Enum-like**: ``obj.state`` has a ``.name`` attribute (e.g.
      ``AgentState.RUNNING``).  The name is compared case-insensitively
      to ``"RUNNING"``.
    * **Constant attribute**: ``obj`` exposes a ``RUNNING`` class
      attribute and ``obj.state == obj.RUNNING``.
    * **String fallback**: ``str(obj.state).upper()`` equals or ends
      with ``"RUNNING"``.

    Args:
        obj: Any object that may expose a ``state`` attribute describing
            its current lifecycle phase.

    Returns:
        bool: ``True`` when the agent is running, ``False`` when the
            state is absent, unrecognised, or indicates a non-running
            phase.
    """
    state = getattr(obj, "state", None)
    if state is None:
        return False

    # Case 1: state is an Enum-like with .name
    name = getattr(state, "name", None)
    if isinstance(name, str):
        return name.upper() == "RUNNING"

    # Case 2: instance has a RUNNING attribute constant and state equals it
    if hasattr(obj, "RUNNING"):
        try:
            if state == getattr(obj, "RUNNING"):
                return True
        except Exception:
            pass

    # Case 3: fallback to string comparison of state
    try:
        return str(state).upper().endswith("RUNNING") or str(state).upper() == "RUNNING"
    except Exception:
        return False


def _run_agent(agent: DocumentMonitorAgent, once: bool = False) -> None:
    """Start *agent* and block until it stops or is interrupted.

    Calls ``agent.start()``, then either runs a single tick (``once=True``)
    or repeatedly invokes ``agent.tick()`` for as long as
    :func:`_agent_is_running` returns ``True``.

    Shutdown is triggered by one of the following:

    * ``once=True`` — a single tick is executed and the agent is stopped.
    * The agent transitions out of the running state on its own.
    * A ``KeyboardInterrupt`` (``SIGINT``) is received, which causes
      ``agent.request_stop()`` to be called before the loop exits.
    * An OS signal handled by :func:`_make_signal_handler` sets the
      agent's internal stop flag, causing :func:`_agent_is_running` to
      return ``False`` on the next iteration.

    ``agent.stop()`` is always called in the ``finally`` block to ensure
    resources are released regardless of how the loop exits.

    Args:
        agent: A started (or about-to-be-started) agent instance that
            exposes ``start()``, ``tick()``, ``request_stop()``, and
            ``stop()`` methods.
        once: If ``True``, run a single tick then return.
    """
    agent.start()
    try:
        if once:
            logger.info("--once flag set: running a single poll cycle then exiting.")
            agent.tick()
            return
        while _agent_is_running(agent):
            agent.tick()
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received; shutting down.")
        agent.stop()
        return
    finally:
        agent.stop()


def main(argv: Optional[list[str]] = None) -> None:
    """Run the document monitor agent from the command line.

    This is the top-level entry point wired up in ``pyproject.toml``.
    It parses arguments, configures logging, builds the agent, registers
    a ``SIGTERM`` handler for graceful shutdown, and hands off to
    :func:`_run_agent`.

    Args:
        argv: Argument list to parse.  When ``None`` (the default),
            :data:`sys.argv` ``[1:]`` is used automatically by
            :mod:`argparse`.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    log_startup_banner(logger, "bamboo-document-monitor")

    # Suppress verbose third-party loggers — model is loaded from local cache
    for _noisy in ("httpx", "httpcore", "huggingface_hub", "sentence_transformers"):
        logging.getLogger(_noisy).setLevel(logging.WARNING)

    # Align sentence_transformers cache with the HuggingFace hub cache so that
    # models downloaded via HF hub are found when running in offline mode.
    # Only set if not already overridden in the environment.
    _hf_hub_cache = os.path.expanduser("~/.cache/huggingface/hub")
    os.environ.setdefault("SENTENCE_TRANSFORMERS_HOME", _hf_hub_cache)
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    os.environ.setdefault("HF_HUB_OFFLINE", "1")

    agent = _build_agent(args)
    signal.signal(signal.SIGTERM, _make_signal_handler(agent))
    _run_agent(agent, once=args.once)


if __name__ == "__main__":
    main(sys.argv[1:])
