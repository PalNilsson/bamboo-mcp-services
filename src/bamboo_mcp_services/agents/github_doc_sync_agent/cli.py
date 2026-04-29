"""Command-line interface for the GitHub documentation sync agent."""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import signal
import sys
import time
from typing import Optional, Sequence

import yaml

from bamboo_mcp_services.agents.github_doc_sync_agent.agent import (
    GithubDocSyncAgent,
    GithubDocSyncConfig,
)
from bamboo_mcp_services.agents.github_doc_sync_agent.github_markdown_sync import (
    RepoConfig,
)
from bamboo_mcp_services.common.cli import log_startup_banner

logger = logging.getLogger(__name__)

#: Log format shared by the console handler and the rotating file handler.
_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s  %(message)s"
_LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

#: Default log file path (relative to CWD; override with --log-file).
_DEFAULT_LOG_FILE = "github-doc-sync-agent.log"

#: Rotating file handler limits — 10 MB per file, keep 5 backups.
_LOG_MAX_BYTES = 10 * 1024 * 1024
_LOG_BACKUP_COUNT = 5

#: Default YAML config path.
_DEFAULT_CONFIG = (
    "src/bamboo_mcp_services/resources/config/github-doc-sync-agent.yaml"
)


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser.

    Returns:
        Configured ArgumentParser instance.
    """
    p = argparse.ArgumentParser(
        prog="bamboo-github-sync",
        description=(
            "Periodic GitHub documentation sync agent.  "
            "Downloads changed .md/.rst files from configured repositories "
            "and writes normalised Markdown to a local directory for RAG ingestion."
        ),
    )
    p.add_argument(
        "--config", "-c",
        default=_DEFAULT_CONFIG,
        metavar="PATH",
        help="Path to YAML configuration file (default: %(default)s)",
    )
    p.add_argument(
        "--log-file",
        default=_DEFAULT_LOG_FILE,
        metavar="PATH",
        help=(
            "Path to the rotating log file (default: %(default)s). "
            "Pass an empty string or /dev/null to disable file logging."
        ),
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Minimum log level for both console and file output (default: %(default)s)",
    )
    p.add_argument(
        "--once",
        action="store_true",
        help="Run a single tick then exit (useful for cron / one-shot invocations).",
    )
    return p


def _configure_logging(log_file: str, log_level: str) -> None:
    """Set up the root logger with a console handler and optional rotating file handler.

    Both handlers share the same format and level.  Third-party libraries that
    produce noisy output at INFO are suppressed to WARNING.

    Args:
        log_file: Path for the rotating log file.  Pass ``""`` or
            ``"/dev/null"`` to skip file logging.
        log_level: String log level, e.g. ``"INFO"``.
    """
    level = getattr(logging, log_level.upper(), logging.INFO)
    formatter = logging.Formatter(fmt=_LOG_FORMAT, datefmt=_LOG_DATE_FORMAT)

    root = logging.getLogger()
    root.setLevel(level)

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    console.setLevel(level)
    root.addHandler(console)

    if log_file and log_file != os.devnull:
        try:
            fh = logging.handlers.RotatingFileHandler(
                log_file,
                maxBytes=_LOG_MAX_BYTES,
                backupCount=_LOG_BACKUP_COUNT,
                encoding="utf-8",
            )
            fh.setFormatter(formatter)
            fh.setLevel(level)
            root.addHandler(fh)
            logging.getLogger(__name__).info(
                "Logging to file: %s (max %d MB, %d backups)",
                os.path.abspath(log_file),
                _LOG_MAX_BYTES // (1024 * 1024),
                _LOG_BACKUP_COUNT,
            )
        except OSError as exc:
            logging.getLogger(__name__).warning(
                "Could not open log file %r: %s — file logging disabled.", log_file, exc
            )

    for _noisy in ("urllib3", "requests", "httpx", "httpcore"):
        logging.getLogger(_noisy).setLevel(logging.WARNING)


def _make_signal_handler(agent: GithubDocSyncAgent):
    """Return a SIGTERM handler that stops the agent gracefully.

    Args:
        agent: The running agent instance to stop on signal.

    Returns:
        Signal handler callable.
    """
    def _handler(signum, frame):
        logger.info("Signal %d received — stopping agent.", signum)
        try:
            agent.stop()
        except Exception:
            logger.exception("Error while stopping agent on signal.")
        sys.exit(0)
    return _handler


def _load_repo_configs(cfg: dict) -> list[RepoConfig]:
    """Parse the ``repos`` list from the loaded YAML config dict.

    Each entry must have a ``name`` and ``destination`` key.  All other
    fields are optional and match the :class:`.RepoConfig` dataclass.

    Args:
        cfg: Top-level parsed YAML dict.

    Returns:
        List of :class:`.RepoConfig` instances (may be empty).

    Raises:
        KeyError: If a repo entry is missing ``name`` or ``destination``.
    """
    return [
        RepoConfig(
            name=entry["name"],
            wiki=entry.get("wiki", False),
            destination=entry["destination"],
            normalized_destination=entry.get("normalized_destination"),
            within_hours=entry.get("within_hours"),
            branch=entry.get("branch"),
            include_patterns=entry.get("include_patterns", []),
            exclude_patterns=entry.get("exclude_patterns", []),
            normalize_for_rag=entry.get("normalize_for_rag", False),
        )
        for entry in cfg.get("repos", [])
    ]


def _load_config_file(config_path: str):
    """Read and parse the YAML config file.

    Args:
        config_path: Path to the YAML configuration file.

    Returns:
        Parsed config dict, or None on error.
    """
    try:
        with open(config_path, "r") as fh:
            cfg = yaml.safe_load(fh)
    except OSError as exc:
        logger.error("Cannot read config file %r: %s", config_path, exc)
        return None
    return cfg if cfg is not None else {}


def _run_agent(agent: GithubDocSyncAgent, once: bool, tick_interval_s: float) -> int:
    """Start the agent, run once or loop, then stop it.

    Args:
        agent: Configured agent instance.
        once: If True, run a single tick then return.
        tick_interval_s: Seconds to sleep between ticks in daemon mode.

    Returns:
        Exit code (0 for success, 1 for unhandled error).
    """
    try:
        agent.start()
        logger.info("Agent started (state=%s)", agent.state.value)

        if once:
            logger.info("--once flag set: running a single tick then exiting.")
            agent.tick()
            h = agent.health()
            logger.info(
                "Tick complete. last_repos_synced=%s  last_error_repo=%s",
                h.details.get("last_repos_synced"),
                h.details.get("last_error_repo") or "none",
            )
        else:
            logger.info(
                "Entering run loop (tick_interval=%.1fs). "
                "Press Ctrl-C or send SIGTERM to stop.",
                tick_interval_s,
            )
            while True:
                agent.tick()
                time.sleep(tick_interval_s)

    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received — shutting down.")
    except Exception:
        logger.exception("Unhandled exception in agent run loop.")
        return 1
    finally:
        try:
            agent.stop()
            logger.info("Agent stopped cleanly (state=%s)", agent.state.value)
        except Exception:
            logger.exception("Error while stopping agent.")

    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point for the GitHub documentation sync agent.

    Parses arguments, configures logging, builds and starts the agent, then
    either runs a single tick (``--once``) or loops indefinitely calling
    ``tick()`` at the configured interval until interrupted.

    A ``GITHUB_TOKEN`` environment variable, if set, is passed to the
    underlying GitHub API calls via the requests session.  The token is
    **not** required for public repositories.

    Args:
        argv: Command-line arguments.  If ``None``, uses ``sys.argv``.

    Returns:
        Exit code (0 for success, 1 for error).
    """
    args = build_parser().parse_args(argv)
    _configure_logging(args.log_file, args.log_level)
    log_startup_banner(logger, "bamboo-github-sync")
    logger.info("Starting (config=%s)", args.config)

    cfg = _load_config_file(args.config)
    if cfg is None:
        return 1

    try:
        repos = _load_repo_configs(cfg)
    except KeyError as exc:
        logger.error(
            "Config file %r has a repo entry missing required key %s",
            args.config,
            exc,
        )
        return 1

    if not repos:
        logger.warning(
            "Config file %r contains no 'repos' entries — agent will run but "
            "never download anything.",
            args.config,
        )

    refresh_interval_s = int(cfg.get("refresh_interval_s", 3600))
    tick_interval_s = float(cfg.get("tick_interval_s", 60.0))

    github_token = os.environ.get("GITHUB_TOKEN")
    if github_token:
        logger.info("GITHUB_TOKEN detected — will be used for GitHub API requests")
    else:
        logger.debug("GITHUB_TOKEN not set; only public repositories will be accessible")

    logger.info(
        "Configuration: repos=%d  refresh_interval=%ds  tick_interval=%.1fs",
        len(repos),
        refresh_interval_s,
        tick_interval_s,
    )
    for r in repos:
        logger.info(
            "  repo: %s  branch=%s  dest=%s  norm_dest=%s",
            r.name,
            r.branch or "<default>",
            r.destination,
            r.normalized_destination or "<none>",
        )

    config = GithubDocSyncConfig(
        repos=repos,
        refresh_interval_s=refresh_interval_s,
        tick_interval_s=tick_interval_s,
    )
    agent = GithubDocSyncAgent(config=config)
    signal.signal(signal.SIGTERM, _make_signal_handler(agent))

    return _run_agent(agent, args.once, tick_interval_s)


if __name__ == "__main__":
    raise SystemExit(main())
