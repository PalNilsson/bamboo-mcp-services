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

"""SupervisorAgent — process-level orchestrator for Bamboo MCP Services.

The supervisor manages all other agents as child subprocesses.  It supports
two modes per agent:

* **daemon** — the child process runs indefinitely; the supervisor monitors it
  and restarts it automatically when it exits, with exponential back-off on
  rapid failures.
* **scheduled** — a short-lived ``--once`` process is launched on a configurable
  interval; the supervisor waits for it to complete, records the exit code, then
  schedules the next run.

The two modes may be mixed freely within a single supervisor configuration.

Future extension point
----------------------
A lightweight HTTP health endpoint (e.g. ``GET /health → JSON``) is a natural
next step once the system is deployed on a remote machine.  The ``health()``
method already returns a fully-populated ``HealthReport`` with per-agent detail;
the HTTP layer would be a thin wrapper that serialises ``agent.health().to_dict()``
on each request.  See ``TODO: HTTP health endpoint`` comments below for the
intended integration points.
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from bamboo_mcp_services.agents.base import Agent
from bamboo_mcp_services.agents.supervisor_agent.scheduler import (
    AgentConfig,
    DaemonState,
    ScheduledState,
    MODE_DAEMON,
    MODE_SCHEDULED,
)

logger = logging.getLogger(__name__)

# ── Supervisor configuration ───────────────────────────────────────────────────

#: Default seconds between health-poll ticks in daemon mode.
DEFAULT_HEALTH_POLL_INTERVAL_S = 30.0

#: Default seconds to wait for a child to exit after SIGTERM before SIGKILL.
DEFAULT_STOP_TIMEOUT_S = 30.0


@dataclass(frozen=True)
class SupervisorConfig:
    """Immutable top-level supervisor configuration.

    Attributes:
        agents: List of per-agent configuration objects.
        health_poll_interval_s: How often (seconds) the supervisor's own tick
            polls daemon-process health and dispatches due scheduled jobs.
        stop_timeout_s: Seconds to wait for a child to exit gracefully after
            SIGTERM before escalating to SIGKILL.

        # TODO: HTTP health endpoint
        # http_host: str = "127.0.0.1"
        # http_port: int = 8765
        # http_enabled: bool = False
    """
    agents: list = field(default_factory=list)
    health_poll_interval_s: float = DEFAULT_HEALTH_POLL_INTERVAL_S
    stop_timeout_s: float = DEFAULT_STOP_TIMEOUT_S


# ── SupervisorAgent ────────────────────────────────────────────────────────────

class SupervisorAgent(Agent):
    """Orchestrates all Bamboo MCP Services agents as child subprocesses.

    Each enabled agent is either run as a long-lived daemon (mode ``"daemon"``)
    or dispatched as a short-lived one-shot on a configurable schedule
    (mode ``"scheduled"``).

    The supervisor itself implements the standard ``Agent`` lifecycle so that it
    can eventually be managed by an outer process monitor (systemd, Docker, etc.)
    using exactly the same interface as the agents it manages.
    """

    def __init__(self, config: SupervisorConfig) -> None:
        """Initialise the supervisor.

        Args:
            config: Fully-populated :class:`SupervisorConfig` instance.
        """
        super().__init__(name="supervisor-agent")
        self._config = config

        # Runtime state keyed by agent name.
        self._daemon_states: dict[str, DaemonState] = {}
        self._scheduled_states: dict[str, ScheduledState] = {}

        # Raw subprocess.Popen handles keyed by agent name (daemon agents).
        self._daemon_procs: dict[str, subprocess.Popen] = {}

    # ── Agent lifecycle ────────────────────────────────────────────────────────

    def _start_impl(self) -> None:
        """Validate config and launch all enabled daemon agents.

        Scheduled agents are not launched here; they are dispatched by
        ``_tick_impl()`` when their interval first elapses.

        Raises:
            ValueError: If any ``AgentConfig`` has an unknown ``mode``.
        """
        for cfg in self._config.agents:
            if not isinstance(cfg, AgentConfig):
                raise TypeError(
                    f"Expected AgentConfig, got {type(cfg).__name__} for entry {cfg!r}"
                )
            if cfg.mode not in (MODE_DAEMON, MODE_SCHEDULED):
                raise ValueError(
                    f"Agent '{cfg.name}': unknown mode {cfg.mode!r}; "
                    f"must be '{MODE_DAEMON}' or '{MODE_SCHEDULED}'"
                )
            if not cfg.enabled:
                logger.info("Agent '%s' is disabled — skipping.", cfg.name)
                continue

            if cfg.mode == MODE_DAEMON:
                state = DaemonState(config=cfg)
                self._daemon_states[cfg.name] = state
                self._wait_for_dependency(cfg)
                self._launch_daemon(cfg.name)

            elif cfg.mode == MODE_SCHEDULED:
                state = ScheduledState(config=cfg)
                self._scheduled_states[cfg.name] = state
                logger.info(
                    "Scheduled agent '%s' registered (interval=%.0fs).",
                    cfg.name,
                    cfg.interval_s or 0,
                )

    def _tick_impl(self) -> None:
        """Perform one supervisor health-poll cycle.

        For daemon agents: check whether the managed process is still alive;
        restart it (with back-off) if it has exited.

        For scheduled agents: dispatch a new one-shot run if the interval has
        elapsed and no run is currently in progress.
        """
        self._poll_daemons()
        self._dispatch_scheduled()

    def _stop_impl(self) -> None:
        """Send SIGTERM to all child processes and wait for them to exit.

        After ``config.stop_timeout_s`` any process that has not exited receives
        SIGKILL.
        """
        # Collect every live process handle.
        all_procs: list[tuple[str, subprocess.Popen]] = []

        for name, proc in list(self._daemon_procs.items()):
            if proc.poll() is None:
                all_procs.append((name, proc))

        for name, state in self._scheduled_states.items():
            # A scheduled one-shot may be in flight.
            if state.running_pid is not None:
                # We don't keep the Popen handle for scheduled processes after
                # wait() returns, so we use os.kill directly.
                try:
                    os.kill(state.running_pid, signal.SIGTERM)
                    logger.info(
                        "Sent SIGTERM to scheduled agent '%s' (pid=%d).",
                        name, state.running_pid,
                    )
                except ProcessLookupError:
                    pass

        # SIGTERM to daemon processes.
        for name, proc in all_procs:
            logger.info(
                "Sending SIGTERM to daemon agent '%s' (pid=%d).", name, proc.pid
            )
            try:
                proc.send_signal(signal.SIGTERM)
            except ProcessLookupError:
                pass

        # Wait up to stop_timeout_s for graceful shutdown.
        deadline = time.monotonic() + self._config.stop_timeout_s
        for name, proc in all_procs:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                proc.wait(timeout=remaining)
                logger.info(
                    "Daemon agent '%s' exited (rc=%d).", name, proc.returncode
                )
            except subprocess.TimeoutExpired:
                logger.warning(
                    "Daemon agent '%s' did not exit within %.0fs — sending SIGKILL.",
                    name, self._config.stop_timeout_s,
                )
                proc.kill()
                proc.wait()

    def _health_details(self) -> Mapping[str, Any]:
        """Return per-agent health details for inclusion in the ``HealthReport``.

        Returns:
            Dictionary with one entry per managed agent, keyed by agent name.

        # TODO: HTTP health endpoint
        # The dict returned here is exactly what the HTTP /health endpoint would
        # serialise as JSON.  A future aiohttp/Flask handler would call:
        #   report = supervisor.health()
        #   return json.dumps(report.to_dict())
        """
        details: dict[str, Any] = {}
        for name, state in self._daemon_states.items():
            details[name] = state.to_health_dict()
        for name, state in self._scheduled_states.items():
            details[name] = state.to_health_dict()
        return {"agents": details}

    # ── Daemon management ──────────────────────────────────────────────────────

    def _launch_daemon(self, name: str) -> None:
        """Start (or restart) the daemon agent with the given name.

        Respects back-off: if the state says it is too soon to restart, this
        method returns without starting a process and the supervisor will retry
        on the next tick.

        Args:
            name: Agent name, must be a key in ``_daemon_states``.
        """
        state = self._daemon_states[name]

        # Enforce back-off.
        if time.monotonic() < state.next_restart_after:
            remaining = state.next_restart_after - time.monotonic()
            logger.debug(
                "Back-off active for '%s': %.0fs remaining before next restart attempt.",
                name, remaining,
            )
            return

        cmd = state.config.command
        logger.info("Starting daemon agent '%s': %s", name, " ".join(str(c) for c in cmd))
        try:
            proc = subprocess.Popen(cmd)
        except (FileNotFoundError, PermissionError) as exc:
            logger.error("Failed to launch daemon agent '%s': %s", name, exc)
            # Schedule a retry with back-off even on launch failure.
            state.record_exit(exit_code=-1)
            backoff = state.compute_backoff_s()
            if backoff > 0:
                state.next_restart_after = time.monotonic() + backoff
                logger.warning(
                    "Will retry daemon agent '%s' in %.0fs (back-off).", name, backoff
                )
            return

        self._daemon_procs[name] = proc
        state.record_start(proc.pid)
        logger.info("Daemon agent '%s' started (pid=%d).", name, proc.pid)

    def _poll_daemons(self) -> None:
        """Check all daemon processes for unexpected exits and restart them."""
        for name, proc in list(self._daemon_procs.items()):
            rc = proc.poll()
            if rc is None:
                # Process is still running — nothing to do.
                continue

            state = self._daemon_states[name]
            logger.warning(
                "Daemon agent '%s' (pid=%d) exited unexpectedly (rc=%d). "
                "Scheduling restart.",
                name, proc.pid, rc,
            )
            state.record_exit(exit_code=rc)

            backoff = state.compute_backoff_s()
            if backoff > 0:
                state.next_restart_after = time.monotonic() + backoff
                logger.warning(
                    "Rapid-restart back-off for '%s': waiting %.0fs before next attempt.",
                    name, backoff,
                )

            self._launch_daemon(name)

    # ── Scheduled management ───────────────────────────────────────────────────

    def _dispatch_scheduled(self) -> None:
        """Dispatch scheduled one-shot agents that are due."""
        for name, state in self._scheduled_states.items():
            if not state.is_due():
                continue

            self._wait_for_dependency(state.config)
            cmd = state.config.command
            logger.info(
                "Dispatching scheduled agent '%s' (run #%d): %s",
                name, state.run_count + 1, " ".join(str(c) for c in cmd),
            )
            try:
                proc = subprocess.Popen(cmd)
            except (FileNotFoundError, PermissionError) as exc:
                logger.error(
                    "Failed to launch scheduled agent '%s': %s", name, exc
                )
                # Record as a failed run and advance the schedule.
                state.record_dispatch(pid=-1)
                state.record_completion(exit_code=-1)
                continue

            state.record_dispatch(proc.pid)
            timeout = state.effective_run_timeout_s()
            try:
                proc.wait(timeout=timeout)
                state.record_completion(proc.returncode)
                level = logging.INFO if proc.returncode == 0 else logging.WARNING
                logger.log(
                    level,
                    "Scheduled agent '%s' finished (rc=%d). Next run in %.0fs.",
                    name, proc.returncode, state.config.interval_s or 0,
                )
            except subprocess.TimeoutExpired:
                logger.error(
                    "Scheduled agent '%s' exceeded timeout (%.0fs) — killing.",
                    name, timeout,
                )
                proc.kill()
                proc.wait()
                state.record_completion(exit_code=-1)

    # ── Dependency helpers ─────────────────────────────────────────────────────

    def _wait_for_dependency(self, cfg: AgentConfig) -> None:
        """Block until ``cfg.depends_on_file`` exists or the timeout elapses.

        If the file does not appear within ``cfg.depends_timeout_s``, the
        method logs a warning and returns so the agent starts anyway — it may
        fall back to its own default configuration (e.g. the ingestion agent
        falling back to its built-in queue list).

        Args:
            cfg: Agent configuration that may contain a ``depends_on_file``.
        """
        if not cfg.depends_on_file:
            return
        if os.path.exists(cfg.depends_on_file):
            return

        logger.info(
            "Agent '%s' waiting for dependency file '%s' (timeout=%.0fs).",
            cfg.name, cfg.depends_on_file, cfg.depends_timeout_s,
        )
        deadline = time.monotonic() + cfg.depends_timeout_s
        while time.monotonic() < deadline:
            time.sleep(2.0)
            if os.path.exists(cfg.depends_on_file):
                logger.info(
                    "Dependency file '%s' found — starting agent '%s'.",
                    cfg.depends_on_file, cfg.name,
                )
                return

        logger.warning(
            "Dependency file '%s' not found after %.0fs — starting agent '%s' anyway.",
            cfg.depends_on_file, cfg.depends_timeout_s, cfg.name,
        )
