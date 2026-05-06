# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
#
# Authors
# - Paul Nilsson, paul.nilsson@cern.ch, 2026

"""Command-line interface for the supervisor agent."""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import signal
import sys
import time
from typing import Optional, Sequence

import yaml

from bamboo_mcp_services.agents.supervisor_agent.agent import (
    SupervisorAgent,
    SupervisorConfig,
)
from bamboo_mcp_services.agents.supervisor_agent.scheduler import (
    AgentConfig,
    MODE_DAEMON,
    MODE_SCHEDULED,
)
from bamboo_mcp_services.common.cli import log_startup_banner

logger = logging.getLogger(__name__)

# ── Logging constants ──────────────────────────────────────────────────────────

_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s  %(message)s"
_LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_DEFAULT_LOG_FILE = "supervisor-agent.log"
_LOG_MAX_BYTES = 10 * 1024 * 1024
_LOG_BACKUP_COUNT = 5

#: Default YAML config path.
_DEFAULT_CONFIG = (
    "src/bamboo_mcp_services/resources/config/supervisor-agent.yaml"
)


# ── Argument parser ────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser.

    Returns:
        Configured ArgumentParser instance.
    """
    p = argparse.ArgumentParser(
        prog="bamboo-supervisor",
        description=(
            "Supervisor agent for Bamboo MCP Services.  "
            "Starts all configured agents as subprocesses, monitors daemon "
            "agents for unexpected exits (restarting them automatically), and "
            "dispatches scheduled one-shot agents on their configured interval."
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
        help="Minimum log level (default: %(default)s)",
    )
    p.add_argument(
        "--once",
        action="store_true",
        help=(
            "Run a single health-poll tick then exit.  "
            "Useful for smoke-testing the configuration without leaving "
            "long-running processes behind."
        ),
    )
    p.add_argument(
        "--status",
        action="store_true",
        help=(
            "Print a JSON health report of currently-running managed processes "
            "and exit without starting or stopping anything.  "
            "Requires the supervisor to already be running (reads config only, "
            "does not start agents)."
        ),
    )
    return p


# ── Logging setup ──────────────────────────────────────────────────────────────

def _configure_logging(log_file: str, log_level: str) -> None:
    """Set up the root logger with a console handler and optional rotating file handler.

    Args:
        log_file: Rotating log file path.  ``""`` or ``"/dev/null"`` disables it.
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


# ── Config loading ─────────────────────────────────────────────────────────────

def _load_config(config_path: str) -> Optional[SupervisorConfig]:
    """Parse the YAML config file into a :class:`SupervisorConfig`.

    Args:
        config_path: Path to the YAML file.

    Returns:
        Populated :class:`SupervisorConfig`, or ``None`` on error.
    """
    try:
        with open(config_path, "r") as fh:
            raw = yaml.safe_load(fh)
    except OSError as exc:
        logger.error("Cannot read config file %r: %s", config_path, exc)
        return None

    if raw is None:
        raw = {}

    agent_cfgs: list[AgentConfig] = []
    for entry in raw.get("agents", []):
        name = entry.get("name")
        if not name:
            logger.warning("Skipping agent entry with no 'name': %r", entry)
            continue

        mode = entry.get("mode", MODE_DAEMON)
        if mode not in (MODE_DAEMON, MODE_SCHEDULED):
            logger.error(
                "Agent '%s': unknown mode %r — must be 'daemon' or 'scheduled'. Skipping.",
                name, mode,
            )
            continue

        command = entry.get("command")
        if not command or not isinstance(command, list):
            logger.error(
                "Agent '%s': 'command' must be a non-empty list. Skipping.", name
            )
            continue

        if mode == MODE_SCHEDULED and not entry.get("interval_s"):
            logger.error(
                "Agent '%s': mode=scheduled requires 'interval_s'. Skipping.", name
            )
            continue

        agent_cfgs.append(AgentConfig(
            name=name,
            mode=mode,
            command=[str(c) for c in command],
            enabled=entry.get("enabled", True),
            interval_s=float(entry["interval_s"]) if entry.get("interval_s") else None,
            depends_on_file=entry.get("depends_on_file"),
            depends_timeout_s=float(entry.get("depends_timeout_s", 120.0)),
            run_timeout_s=(
                float(entry["run_timeout_s"]) if entry.get("run_timeout_s") else None
            ),
        ))

    return SupervisorConfig(
        agents=agent_cfgs,
        health_poll_interval_s=float(raw.get("health_poll_interval_s", 30.0)),
        stop_timeout_s=float(raw.get("stop_timeout_s", 30.0)),
    )


# ── Signal handler ─────────────────────────────────────────────────────────────

def _make_signal_handler(agent: SupervisorAgent):
    """Return a SIGTERM handler that stops the supervisor (and its children) gracefully.

    Args:
        agent: Running supervisor instance.

    Returns:
        Signal handler callable.
    """
    def _handler(signum, frame):
        logger.info("Signal %d received — stopping supervisor and all agents.", signum)
        try:
            agent.stop()
        except Exception:
            logger.exception("Error while stopping supervisor on signal.")
        sys.exit(0)
    return _handler


# ── Status command ─────────────────────────────────────────────────────────────

def _print_status(config_path: str) -> int:
    """Load config and print a JSON status skeleton, then exit.

    This is a lightweight inspection mode: it shows what *would* be managed
    without actually starting anything.

    Args:
        config_path: Path to the YAML config file.

    Returns:
        Exit code.
    """
    cfg = _load_config(config_path)
    if cfg is None:
        return 1

    status = {
        "config": config_path,
        "health_poll_interval_s": cfg.health_poll_interval_s,
        "agents": [
            {
                "name": a.name,
                "mode": a.mode,
                "enabled": a.enabled,
                "interval_s": a.interval_s,
                "depends_on_file": a.depends_on_file,
                "command": a.command,
            }
            for a in cfg.agents
        ],
    }
    print(json.dumps(status, indent=2))
    return 0


# ── Main entry point ───────────────────────────────────────────────────────────

def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point for the supervisor agent.

    Parses arguments, configures logging, builds and starts the supervisor,
    then either runs a single tick (``--once``) or loops indefinitely until
    interrupted.

    Args:
        argv: Command-line arguments.  If ``None``, uses ``sys.argv``.

    Returns:
        Exit code (0 for success, 1 for error).
    """
    args = build_parser().parse_args(argv)
    _configure_logging(args.log_file, args.log_level)
    log_startup_banner(logger, "bamboo-supervisor")

    if args.status:
        return _print_status(args.config)

    logger.info("Starting (config=%s)", args.config)

    cfg = _load_config(args.config)
    if cfg is None:
        return 1

    enabled = [a for a in cfg.agents if a.enabled]
    disabled = [a for a in cfg.agents if not a.enabled]
    logger.info(
        "Configuration: %d agent(s) enabled, %d disabled.  "
        "health_poll_interval=%.0fs  stop_timeout=%.0fs",
        len(enabled), len(disabled),
        cfg.health_poll_interval_s, cfg.stop_timeout_s,
    )
    for a in enabled:
        if a.mode == MODE_DAEMON:
            logger.info("  [daemon]    %s: %s", a.name, " ".join(a.command))
        else:
            logger.info(
                "  [scheduled] %s (every %.0fs): %s",
                a.name, a.interval_s or 0, " ".join(a.command),
            )
    for a in disabled:
        logger.info("  [disabled]  %s", a.name)

    supervisor = SupervisorAgent(config=cfg)
    signal.signal(signal.SIGTERM, _make_signal_handler(supervisor))

    try:
        supervisor.start()
        logger.info("Supervisor started (state=%s)", supervisor.state.value)

        if args.once:
            logger.info("--once flag set: running a single health-poll tick then exiting.")
            supervisor.tick()
            health = supervisor.health()
            logger.info("Tick complete. Health: %s", json.dumps(health.to_dict(), indent=2))
        else:
            logger.info(
                "Entering run loop (poll_interval=%.0fs). "
                "Press Ctrl-C or send SIGTERM to stop.",
                cfg.health_poll_interval_s,
            )
            while True:
                supervisor.tick()
                time.sleep(cfg.health_poll_interval_s)

    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received — shutting down.")
    except Exception:
        logger.exception("Unhandled exception in supervisor run loop.")
        return 1
    finally:
        try:
            supervisor.stop()
            logger.info("Supervisor stopped cleanly (state=%s)", supervisor.state.value)
        except Exception:
            logger.exception("Error while stopping supervisor.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
