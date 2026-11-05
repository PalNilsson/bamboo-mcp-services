"""Command-line interface for the ingestion agent."""
from __future__ import annotations

import argparse
import dataclasses
import logging
import logging.handlers
import os
import signal
import sys
import time
import yaml
from typing import Optional, Sequence

from bamboo_mcp_services.agents.ingestion_agent.agent import (
    IngestionAgent,
    IngestionAgentConfig,
    SourceConfig,
    BigPandaJobsConfig,
)
from bamboo_mcp_services.common.cli import log_startup_banner
from bamboo_mcp_services.agents.ingestion_agent.bigpanda_jobs_fetcher import (
    DEFAULT_QUEUES,
    DEFAULT_CYCLE_INTERVAL_S,
    DEFAULT_INTER_QUEUE_DELAY_S,
)

logger = logging.getLogger(__name__)

#: Log format used for both the console handler and the file handler.
_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s  %(message)s"
_LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

#: Default log file path (relative to CWD; override with --log-file).
_DEFAULT_LOG_FILE = "ingestion-agent.log"

#: Rotating file handler limits — 10 MB per file, keep 5 backups.
_LOG_MAX_BYTES = 10 * 1024 * 1024
_LOG_BACKUP_COUNT = 5


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser.

    Returns:
        Configured ArgumentParser instance.
    """
    p = argparse.ArgumentParser(
        prog='bamboo-ingestion',
        description='Periodic BigPanda / PanDA data ingestion agent.',
    )
    p.add_argument(
        '--config', '-c',
        default='src/bamboo_mcp_services/resources/config/ingestion-agent.yaml',
        help='Path to YAML configuration file (default: %(default)s)',
    )
    p.add_argument(
        '--log-file',
        default=_DEFAULT_LOG_FILE,
        metavar='PATH',
        help='Path to the rotating log file (default: %(default)s). '
             'Pass an empty string or /dev/null to disable file logging.',
    )
    p.add_argument(
        '--log-level',
        default='INFO',
        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'],
        help='Minimum log level for both console and file output (default: %(default)s)',
    )
    p.add_argument(
        '--once',
        action='store_true',
        help='Run a single tick then exit (useful for cron / one-shot invocations)',
    )
    p.add_argument(
        '--inter-queue-delay',
        type=int,
        default=None,
        metavar='SECONDS',
        help='Override the inter-queue sleep duration in seconds (overrides the YAML value). '
             'Set to 0 to disable the delay entirely, e.g. during debugging.',
    )
    p.add_argument(
        '--max-queues',
        type=int,
        default=None,
        metavar='N',
        help='Override the maximum number of queues to process per cycle (overrides the YAML '
             'value). Set to 0 to process all available queues. Useful for quick test runs '
             'when cric_path points to the full ~700-queue CRIC file.',
    )
    return p


def _configure_logging(log_file: str, log_level: str) -> None:
    """Set up the root logger with a console handler and an optional rotating file handler.

    Both handlers share the same format and level.  Third-party libraries that
    produce noisy output at INFO are suppressed to WARNING.

    Args:
        log_file: Path for the rotating log file.  Pass ``""`` or ``"/dev/null"``
            to skip file logging.
        log_level: String log level, e.g. ``"INFO"``.
    """
    level = getattr(logging, log_level.upper(), logging.INFO)

    formatter = logging.Formatter(fmt=_LOG_FORMAT, datefmt=_LOG_DATE_FORMAT)

    root = logging.getLogger()
    root.setLevel(level)

    # Console handler — always present.
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    console.setLevel(level)
    root.addHandler(console)

    # Rotating file handler — optional.
    if log_file and log_file != os.devnull:
        try:
            fh = logging.handlers.RotatingFileHandler(
                log_file,
                maxBytes=_LOG_MAX_BYTES,
                backupCount=_LOG_BACKUP_COUNT,
                encoding='utf-8',
            )
            fh.setFormatter(formatter)
            fh.setLevel(level)
            root.addHandler(fh)
            # Log this after both handlers are attached so it appears in the file too.
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

    # Quiet down noisy third-party libraries.
    for _noisy in ("urllib3", "requests", "httpx", "httpcore"):
        logging.getLogger(_noisy).setLevel(logging.WARNING)


def _bigpanda_jobs_config_from_dict(cfg: dict) -> BigPandaJobsConfig:
    """Build a :class:`BigPandaJobsConfig` from the ``bigpanda_jobs`` YAML section.

    Args:
        cfg: Parsed YAML dict (may be empty or missing keys).

    Returns:
        Populated :class:`BigPandaJobsConfig`.
    """
    return BigPandaJobsConfig(
        enabled=cfg.get('enabled', True),
        queues=cfg.get('queues', list(DEFAULT_QUEUES)),
        cric_path=cfg.get('cric_path', None),
        max_queues=cfg.get('max_queues', 0),
        cycle_interval_s=cfg.get('cycle_interval_s', DEFAULT_CYCLE_INTERVAL_S),
        inter_queue_delay_s=cfg.get('inter_queue_delay_s', DEFAULT_INTER_QUEUE_DELAY_S),
        retention_days=int(cfg.get('retention_days', 0)),
    )


def _make_signal_handler(agent: IngestionAgent):
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


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point for the ingestion agent.

    Parses arguments, configures logging (stderr + rotating file), builds and
    starts the agent, then either runs a single tick (``--once``) or loops
    indefinitely calling ``tick()`` at the configured interval until interrupted.

    Args:
        argv: Command-line arguments.  If ``None``, uses ``sys.argv``.

    Returns:
        Exit code (0 for success, 1 for error).
    """
    args = build_parser().parse_args(argv)
    _configure_logging(args.log_file, args.log_level)
    log_startup_banner(logger, "bamboo-ingestion")
    logger.info("Starting (config=%s)", args.config)

    try:
        with open(args.config, 'r') as fh:
            cfg = yaml.safe_load(fh)
    except OSError as exc:
        logger.error("Cannot read config file %r: %s", args.config, exc)
        return 1

    sources = [SourceConfig(**s) for s in cfg.get('sources', [])]
    bpjobs_cfg = _bigpanda_jobs_config_from_dict(cfg.get('bigpanda_jobs', {}))

    # CLI flags override YAML values where provided.
    if args.inter_queue_delay is not None:
        logger.info(
            "Overriding inter_queue_delay_s: %ds (YAML value was %ds)",
            args.inter_queue_delay,
            bpjobs_cfg.inter_queue_delay_s,
        )
        bpjobs_cfg = dataclasses.replace(bpjobs_cfg, inter_queue_delay_s=args.inter_queue_delay)
    if args.max_queues is not None:
        logger.info(
            "Overriding max_queues: %d (YAML value was %d)",
            args.max_queues,
            bpjobs_cfg.max_queues,
        )
        bpjobs_cfg = dataclasses.replace(bpjobs_cfg, max_queues=args.max_queues)
    duckdb_path = cfg.get('duckdb_path', ':memory:')
    tick_interval_s = float(cfg.get('tick_interval_s', 1.0))

    logger.info(
        "Configuration: duckdb_path=%s  tick_interval=%.1fs  "
        "bigpanda_jobs.enabled=%s  bigpanda_jobs.cric_path=%s  "
        "bigpanda_jobs.queues=%s  bigpanda_jobs.max_queues=%s  "
        "bigpanda_jobs.cycle_interval=%ds  bigpanda_jobs.inter_queue_delay=%ds  "
        "bigpanda_jobs.retention_days=%s",
        duckdb_path,
        tick_interval_s,
        bpjobs_cfg.enabled,
        bpjobs_cfg.cric_path,
        bpjobs_cfg.queues,
        bpjobs_cfg.max_queues if bpjobs_cfg.max_queues else "unlimited",
        bpjobs_cfg.cycle_interval_s,
        bpjobs_cfg.inter_queue_delay_s,
        bpjobs_cfg.retention_days or "off",
    )

    mac = IngestionAgentConfig(
        sources=sources,
        duckdb_path=duckdb_path,
        tick_interval_s=tick_interval_s,
        bigpanda_jobs=bpjobs_cfg,
    )
    agent = IngestionAgent(config=mac)
    signal.signal(signal.SIGTERM, _make_signal_handler(agent))

    try:
        agent.start()
        logger.info("Agent started (state=%s)", agent.state.value)

        if args.once:
            logger.info("--once flag set: running a single tick (inter-queue delay skipped) then exiting.")
            agent.tick_once()
            logger.info("Tick complete.")
        else:
            logger.info(
                "Entering run loop (tick_interval=%.1fs). Press Ctrl-C or send SIGTERM to stop.",
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


if __name__ == '__main__':
    raise SystemExit(main())
