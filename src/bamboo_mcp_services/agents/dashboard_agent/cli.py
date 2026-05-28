"""Command-line interface for the dashboard agent."""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import signal
import sys
import time
from typing import Optional, Sequence

from bamboo_mcp_services.agents.dashboard_agent.agent import DashboardAgent, DashboardConfig
from bamboo_mcp_services.common.cli import log_startup_banner

logger = logging.getLogger(__name__)

_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s  %(message)s"
_LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_DEFAULT_LOG_FILE = "dashboard-agent.log"
_LOG_MAX_BYTES = 10 * 1024 * 1024
_LOG_BACKUP_COUNT = 5


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser.

    Returns:
        Configured ArgumentParser instance.
    """
    p = argparse.ArgumentParser(
        prog="bamboo-dashboard",
        description="Bamboo monitoring dashboard — serves a web UI for job and queue metrics.",
    )
    p.add_argument(
        "--jobs-db",
        default="jobs.duckdb",
        metavar="PATH",
        help="Path to jobs.duckdb (default: %(default)s)",
    )
    p.add_argument(
        "--cric-db",
        default="cric.duckdb",
        metavar="PATH",
        help="Path to CRIC DuckDB file (default: %(default)s)",
    )
    p.add_argument(
        "--host",
        default="0.0.0.0",
        metavar="ADDR",
        help="HTTP server bind address (default: %(default)s)",
    )
    p.add_argument(
        "--port",
        default=8080,
        type=int,
        metavar="PORT",
        help="HTTP server port (default: %(default)s)",
    )
    p.add_argument(
        "--refresh",
        default=30,
        type=int,
        metavar="SECONDS",
        help="Dashboard auto-refresh interval in seconds (default: %(default)s)",
    )
    p.add_argument(
        "--tick-interval",
        default=15.0,
        type=float,
        metavar="SECONDS",
        help="Agent health-check interval in seconds (default: %(default)s)",
    )
    p.add_argument(
        "--log-file",
        default=_DEFAULT_LOG_FILE,
        metavar="PATH",
        help="Rotating log file path (default: %(default)s). Pass '' to disable.",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level (default: %(default)s)",
    )
    p.add_argument(
        "--once",
        action="store_true",
        help="Start server, verify it is running, print URL, then exit.",
    )
    return p


def _configure_logging(log_file: str, log_level: str) -> None:
    """Configure root logger with console and optional rotating file handler.

    Args:
        log_file: Path for the rotating log file. Pass ``""`` to skip.
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
        except OSError as exc:
            logging.getLogger(__name__).warning(
                "Could not open log file %r: %s — file logging disabled.", log_file, exc
            )

    for _noisy in ("urllib3", "requests", "httpx", "httpcore", "uvicorn"):
        logging.getLogger(_noisy).setLevel(logging.WARNING)


def _make_signal_handler(agent: DashboardAgent):
    """Return a SIGTERM handler that stops the agent gracefully.

    Args:
        agent: The running agent instance.

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


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point for the dashboard agent.

    Args:
        argv: Command-line arguments. If None, uses sys.argv.

    Returns:
        Exit code (0 for success, 1 for error).
    """
    args = build_parser().parse_args(argv)
    _configure_logging(args.log_file, args.log_level)
    log_startup_banner(logger, "bamboo-dashboard")

    config = DashboardConfig(
        jobs_db=args.jobs_db,
        cric_db=args.cric_db,
        host=args.host,
        port=args.port,
        refresh_interval_s=args.refresh,
    )
    logger.info(
        "Configuration: jobs_db=%s  cric_db=%s  host=%s  port=%d  refresh=%ds",
        config.jobs_db,
        config.cric_db,
        config.host,
        config.port,
        config.refresh_interval_s,
    )

    agent = DashboardAgent(config=config)
    signal.signal(signal.SIGTERM, _make_signal_handler(agent))

    try:
        agent.start()
        logger.info("Agent started — dashboard at http://%s:%d", config.host, config.port)

        if args.once:
            logger.info("--once flag set: verifying server is up then exiting.")
            time.sleep(1.0)
            agent.tick()
            print(f"Dashboard running at http://{config.host}:{config.port}", flush=True)
        else:
            logger.info(
                "Serving dashboard (tick_interval=%.1fs). Press Ctrl-C or SIGTERM to stop.",
                args.tick_interval,
            )
            while True:
                agent.tick()
                time.sleep(args.tick_interval)

    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt — shutting down.")
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


if __name__ == "__main__":
    raise SystemExit(main())
