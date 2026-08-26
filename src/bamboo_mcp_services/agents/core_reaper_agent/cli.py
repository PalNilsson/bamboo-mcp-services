"""Command-line interface for the core-dump workspace reaper.

Report-only: this command never removes anything.  There is deliberately no
``--apply`` flag.  See the module docstring in ``agent.py`` for the contract
and the test that enforces it.

Exit codes:
    0  Sweep completed; usage is below the pressure threshold.
    1  Sweep completed with one or more non-fatal errors, or an unhandled
       exception occurred.
    2  Usage error (bad arguments or unreadable config file).
    3  Sweep completed, but usage is at or above the pressure threshold and
       reclaimable space was found — someone needs to free space by hand.
"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import signal
import sys
import time
from typing import Any, Dict, Optional, Sequence

import yaml

from bamboo_mcp_services.agents.core_reaper_agent.agent import (
    ALLOWED_PREFIXES,
    CoreReaperAgent,
    CoreReaperConfig,
    human_bytes,
    resolve_quota_bytes,
    resolve_root,
)
from bamboo_mcp_services.common.cli import log_startup_banner

logger = logging.getLogger(__name__)

#: Log format shared by the console handler and the rotating file handler.
_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s  %(message)s"
_LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

#: Default log file path (relative to CWD; override with --log-file).
_DEFAULT_LOG_FILE = "core-reaper-agent.log"

#: Rotating file handler limits — 10 MB per file, keep 5 backups.
_LOG_MAX_BYTES = 10 * 1024 * 1024
_LOG_BACKUP_COUNT = 5

#: Default YAML config path.
_DEFAULT_CONFIG = "src/bamboo_mcp_services/resources/config/core-reaper-agent.yaml"

#: Exit codes.
EXIT_OK = 0
EXIT_ERRORS = 1
EXIT_USAGE = 2
EXIT_PRESSURE = 3


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser.

    Returns:
        Configured ArgumentParser instance.
    """
    p = argparse.ArgumentParser(
        prog="bamboo-core-reaper",
        description=(
            "Report reclaimable PanDA core-dump analysis workspaces.  "
            "This version removes nothing — it only logs what it could have removed."
        ),
    )
    p.add_argument(
        "--config", "-c",
        default=_DEFAULT_CONFIG,
        metavar="PATH",
        help="Path to YAML configuration file (default: %(default)s).  Missing file is not an error.",
    )
    p.add_argument(
        "--root",
        default=None,
        metavar="PATH",
        help=f"Analysis root to sweep.  Defaults to the config file, then ${{BAMBOO_CORE_ANALYSIS_ROOT}}, "
             f"then /tmp/bamboo/core-analysis.  Must live under {list(ALLOWED_PREFIXES)}.",
    )
    p.add_argument(
        "--min-age-hours",
        type=float,
        default=None,
        metavar="H",
        help="Minimum age of a terminal workspace before it is reported (default: 1.0).",
    )
    p.add_argument(
        "--orphan-age-hours",
        type=float,
        default=None,
        metavar="H",
        help="Longer threshold for workspaces with no manifest at all (default: 24.0).",
    )
    p.add_argument(
        "--mode",
        choices=["prune", "purge"],
        default=None,
        help="prune: report <workspace>/job only (default).  purge: report the whole workspace.",
    )
    p.add_argument(
        "--purge-failed-after-hours",
        type=float,
        default=None,
        metavar="H",
        help="In prune mode, escalate to whole-workspace removal for failed runs older than H hours.",
    )
    p.add_argument(
        "--quota-bytes",
        type=int,
        default=None,
        metavar="N",
        help="Quota ceiling for pressure calculations.  Defaults to $BAMBOO_CORE_ANALYSIS_QUOTA_BYTES or 50 GiB.",
    )
    p.add_argument(
        "--pressure-pct",
        type=float,
        default=None,
        metavar="PCT",
        help="Usage percentage of the quota at which the pressure pass engages (default: 80).",
    )
    p.add_argument(
        "--target-pct",
        type=float,
        default=None,
        metavar="PCT",
        help="Usage percentage the pressure pass aims to get below (default: 60).",
    )
    p.add_argument(
        "--min-age-floor-hours",
        type=float,
        default=None,
        metavar="H",
        help="Hard age floor the pressure pass may never go below (default: 0.5).",
    )
    p.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Summary format written to stdout after each sweep (default: %(default)s).",
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
        help="Run a single sweep then exit (useful for cron / one-shot invocations).",
    )
    return p


def _configure_logging(log_file: str, log_level: str) -> None:
    """Set up the root logger with a console handler and an optional rotating file handler.

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


def _load_yaml(path: str) -> Optional[Dict[str, Any]]:
    """Load the YAML config file.

    A missing file is not an error — every setting has a default and can be
    supplied on the command line.

    Args:
        path: Path to the YAML file.

    Returns:
        Parsed mapping, or ``None`` when the file could not be read or parsed.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except FileNotFoundError:
        logger.info("Config file %s not found — using defaults and command-line flags.", path)
        return {}
    except (OSError, yaml.YAMLError) as exc:
        logger.error("Cannot read config file %r: %s", path, exc)
        return None
    if data is None:
        return {}
    if not isinstance(data, dict):
        logger.error("Config file %r must contain a mapping at the top level.", path)
        return None
    return data


def _pick(cli_value: Any, cfg: Dict[str, Any], key: str, default: Any) -> Any:
    """Resolve one setting from the CLI, then the config file, then the default.

    Args:
        cli_value: Value supplied on the command line, or ``None``.
        cfg: Parsed config mapping.
        key: Config key to consult.
        default: Fallback value.

    Returns:
        The resolved value.
    """
    if cli_value is not None:
        return cli_value
    if key in cfg and cfg[key] is not None:
        return cfg[key]
    return default


def build_config(args: argparse.Namespace, cfg: Dict[str, Any]) -> CoreReaperConfig:
    """Build the reaper configuration from CLI arguments and the config file.

    Args:
        args: Parsed command-line arguments.
        cfg: Parsed config mapping.

    Returns:
        A :class:`CoreReaperConfig`.
    """
    root = resolve_root(_pick(args.root, cfg, "root", None))
    quota = resolve_quota_bytes(_pick(args.quota_bytes, cfg, "quota_bytes", None))
    return CoreReaperConfig(
        root=root,
        min_age_hours=float(_pick(args.min_age_hours, cfg, "min_age_hours", 1.0)),
        orphan_age_hours=float(_pick(args.orphan_age_hours, cfg, "orphan_age_hours", 24.0)),
        mode=str(_pick(args.mode, cfg, "mode", "prune")),
        purge_failed_after_hours=_optional_float(_pick(args.purge_failed_after_hours, cfg, "purge_failed_after_hours", None)),
        quota_bytes=int(quota),
        pressure_pct=float(_pick(args.pressure_pct, cfg, "pressure_pct", 80.0)),
        target_pct=float(_pick(args.target_pct, cfg, "target_pct", 60.0)),
        min_age_floor_hours=float(_pick(args.min_age_floor_hours, cfg, "min_age_floor_hours", 0.5)),
        tick_interval_s=float(cfg.get("tick_interval_s", 3600.0) or 3600.0),
    )


def _optional_float(value: Any) -> Optional[float]:
    """Coerce an optional numeric setting to ``float``.

    Args:
        value: Value from the CLI or config file, possibly ``None``.

    Returns:
        Float value, or ``None``.
    """
    return None if value is None else float(value)


def _make_signal_handler(agent: CoreReaperAgent):
    """Return a SIGTERM handler that stops the agent gracefully.

    Args:
        agent: The running agent instance to stop on signal.

    Returns:
        Signal handler callable.
    """
    def _handler(signum, frame):
        logger.info("Signal %d received — stopping.", signum)
        try:
            agent.stop()
        except Exception:
            logger.exception("Error while stopping on signal.")
        sys.exit(0)
    return _handler


def _print_summary(agent: CoreReaperAgent, fmt: str) -> None:
    """Write the sweep summary to stdout.

    Args:
        agent: Agent holding the completed sweep report.
        fmt: ``'text'`` or ``'json'``.
    """
    report = agent.last_report
    if report is None:
        return
    if fmt == "json":
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
        return
    print(f"Root:         {report.root}")
    print(f"Usage:        {human_bytes(report.usage_bytes)} ({report.usage_pct:.1f}% of {human_bytes(report.quota_bytes)})")
    print(f"Workspaces:   {report.workspaces_scanned}")
    print(f"Reclaimable:  {human_bytes(report.reclaimable_bytes)} across {len(report.candidates)} candidate(s)")
    for candidate in report.candidates:
        print(
            f"  could have removed  {candidate['path']}"
            f"  [{candidate['mode']} {human_bytes(candidate['bytes'])} {candidate['reason']}]"
        )
    if report.refused:
        print(f"Refused by guard: {len(report.refused)}")
        for refusal in report.refused:
            print(f"  refused  {refusal['path']}  [{refusal['guard']}]")
    if report.errors:
        print(f"Errors: {len(report.errors)}")
    print("Nothing was removed — this build has no deletion code.")


def _exit_code(agent: CoreReaperAgent) -> int:
    """Derive the process exit code from the last sweep.

    Args:
        agent: Agent holding the completed sweep report.

    Returns:
        One of :data:`EXIT_OK`, :data:`EXIT_ERRORS`, :data:`EXIT_PRESSURE`.
    """
    report = agent.last_report
    if report is None:
        return EXIT_OK
    if report.errors:
        return EXIT_ERRORS
    threshold = report.quota_bytes * agent.config.pressure_pct / 100.0
    if report.quota_bytes > 0 and report.usage_bytes >= threshold and report.reclaimable_bytes > 0:
        return EXIT_PRESSURE
    return EXIT_OK


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point for the core-dump workspace reaper.

    Args:
        argv: Command-line arguments.  If ``None``, uses ``sys.argv``.

    Returns:
        Exit code; see the module docstring.
    """
    args = build_parser().parse_args(argv)
    _configure_logging(args.log_file, args.log_level)
    log_startup_banner(logger, "bamboo-core-reaper")

    cfg = _load_yaml(args.config)
    if cfg is None:
        return EXIT_USAGE

    try:
        config = build_config(args, cfg)
    except (TypeError, ValueError) as exc:
        logger.error("Invalid configuration: %s", exc)
        return EXIT_USAGE

    agent = CoreReaperAgent(config=config)
    signal.signal(signal.SIGTERM, _make_signal_handler(agent))

    try:
        agent.start()
        if args.once:
            logger.info("--once flag set: running a single sweep then exiting.")
            agent.tick()
            _print_summary(agent, args.format)
            return _exit_code(agent)

        logger.info(
            "Entering run loop (tick_interval=%.1fs).  Press Ctrl-C or send SIGTERM to stop.",
            config.tick_interval_s,
        )
        while True:
            agent.tick()
            _print_summary(agent, args.format)
            time.sleep(config.tick_interval_s)

    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received — shutting down.")
    except Exception:
        logger.exception("Unhandled exception during sweep.")
        return EXIT_ERRORS
    finally:
        try:
            agent.stop()
        except Exception:
            logger.exception("Error while stopping.")

    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
