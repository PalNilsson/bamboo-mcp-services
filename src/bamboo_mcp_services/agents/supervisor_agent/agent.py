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
  repeated fast failures (see :mod:`.scheduler`).
* **scheduled** — a short-lived ``--once`` process is launched on a configurable
  interval; the supervisor records its exit code when it finishes, then
  schedules the next run.

The two modes may be mixed freely within a single supervisor configuration.

Nothing in a tick waits for a child.  Scheduled runs are dispatched and then
reaped on later ticks, so a long one-shot (a GitHub sync can take minutes)
never delays the health check of the daemons.  The cost is granularity: a
scheduled run's completion, and its timeout, are noticed at the first tick
after they happen, i.e. up to ``health_poll_interval_s`` late.

Daemon state drives restarts, not the presence of a process handle: a daemon
whose launch failed (for example, a command that is not on ``PATH``) has no
process at all, and is retried with back-off like any other failure.

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
    describe_exit,
)

logger = logging.getLogger(__name__)

# ── Supervisor configuration ───────────────────────────────────────────────────

#: Default seconds between health-poll ticks in daemon mode.
DEFAULT_HEALTH_POLL_INTERVAL_S = 30.0

#: Default seconds to wait for a child to exit after SIGTERM before SIGKILL.
DEFAULT_STOP_TIMEOUT_S = 30.0

#: Seconds to wait for a timed-out scheduled run to die after SIGKILL.
_KILL_REAP_TIMEOUT_S = 5.0


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

        # Popen handles of live children, keyed by agent name.  An entry is
        # removed as soon as its exit has been recorded, so an exit is never
        # recorded twice.
        self._daemon_procs: dict[str, subprocess.Popen] = {}
        self._scheduled_procs: dict[str, subprocess.Popen] = {}

    # ── Agent lifecycle ────────────────────────────────────────────────────────

    def _start_impl(self) -> None:
        """Validate config and launch all enabled daemon agents.

        Scheduled agents are not launched here; they are dispatched by
        ``_tick_impl()`` when they are first due — on the first tick.

        Raises:
            TypeError: If an entry is not an :class:`AgentConfig`.
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
                self._daemon_states[cfg.name] = DaemonState(config=cfg)
                self._wait_for_dependency(cfg)
                self._launch_daemon(cfg.name)

            elif cfg.mode == MODE_SCHEDULED:
                self._scheduled_states[cfg.name] = ScheduledState(config=cfg)
                logger.info(
                    "Scheduled agent '%s' registered (interval=%.0fs).",
                    cfg.name,
                    cfg.interval_s or 0,
                )

    def _tick_impl(self) -> None:
        """Perform one supervisor health-poll cycle.

        Returns without waiting for any child: daemons are polled, finished
        or timed-out scheduled runs are reaped, and due scheduled runs are
        dispatched.
        """
        self._poll_daemons()
        self._reap_scheduled()
        self._dispatch_scheduled()

    def _stop_impl(self) -> None:
        """Send SIGTERM to all child processes and wait for them to exit.

        Any child still running ``config.stop_timeout_s`` after the SIGTERM
        receives SIGKILL.
        """
        live = [
            (name, proc)
            for procs in (self._daemon_procs, self._scheduled_procs)
            for name, proc in procs.items()
            if proc.poll() is None
        ]
        for name, proc in live:
            logger.info("Sending SIGTERM to agent '%s' (pid=%d).", name, proc.pid)
            try:
                proc.send_signal(signal.SIGTERM)
            except ProcessLookupError:
                pass
        self._wait_for_children(live)
        self._daemon_procs.clear()
        self._scheduled_procs.clear()

    def _wait_for_children(self, live: list) -> None:
        """Wait for children to exit; SIGKILL any still running at the deadline.

        A child killed by the SIGTERM itself (negative return code) is logged
        as a warning: it did not get to shut down cleanly, typically because
        it had not yet installed its SIGTERM handler.

        Args:
            live: ``(name, Popen)`` pairs that were sent SIGTERM.
        """
        deadline = time.monotonic() + self._config.stop_timeout_s
        for name, proc in live:
            try:
                proc.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                logger.warning(
                    "Agent '%s' (pid=%d) did not exit within %.0fs of SIGTERM — sending SIGKILL.",
                    name, proc.pid, self._config.stop_timeout_s,
                )
                proc.kill()
                try:
                    proc.wait(timeout=_KILL_REAP_TIMEOUT_S)
                except subprocess.TimeoutExpired:
                    logger.error("Agent '%s' (pid=%d) survived SIGKILL.", name, proc.pid)
                continue
            if proc.returncode is not None and proc.returncode < 0:
                logger.warning(
                    "Agent '%s' (pid=%d) %s before it could shut down cleanly.",
                    name, proc.pid, describe_exit(proc.returncode),
                )
            else:
                logger.info("Agent '%s' %s.", name, describe_exit(proc.returncode))

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

    # ── Queries used by ``--once`` ─────────────────────────────────────────────

    @property
    def daemon_names(self) -> list[str]:
        """Names of the enabled daemon agents, in configuration order."""
        return list(self._daemon_states)

    def daemons_not_running(self) -> list[str]:
        """Return the names of enabled daemons that have no live process.

        Returns:
            Agent names, in configuration order.
        """
        return [
            name for name in self._daemon_states
            if name not in self._daemon_procs or self._daemon_procs[name].poll() is not None
        ]

    def wait_for_scheduled(self, poll_s: float = 1.0) -> None:
        """Block until every in-flight scheduled run has been reaped.

        Run timeouts still apply: an overdue run is killed by the reaper.

        Args:
            poll_s: Seconds between checks.
        """
        while self._scheduled_procs:
            time.sleep(poll_s)
            self._reap_scheduled()

    # ── Daemon management ──────────────────────────────────────────────────────

    def _launch_daemon(self, name: str) -> None:
        """Start (or restart) the daemon agent with the given name.

        A launch failure is recorded as a fast failure and the next attempt is
        scheduled with back-off; ``_poll_daemons`` makes it.

        Args:
            name: Agent name, must be a key in ``_daemon_states``.
        """
        state = self._daemon_states[name]
        cmd = state.config.command
        logger.info("Starting daemon agent '%s': %s", name, " ".join(str(c) for c in cmd))
        try:
            proc = subprocess.Popen(cmd)
        except (FileNotFoundError, PermissionError) as exc:
            state.record_exit(exit_code=-1)
            backoff = state.schedule_restart()
            logger.error(
                "Failed to launch daemon agent '%s': %s — retrying %s.",
                name, exc, f"in {backoff:.0f}s" if backoff > 0 else "at the next tick",
            )
            return

        self._daemon_procs[name] = proc
        state.record_start(proc.pid)
        logger.info("Daemon agent '%s' started (pid=%d).", name, proc.pid)

    def _poll_daemons(self) -> None:
        """Record daemon exits and (re)launch every daemon that is due.

        Each exit is recorded exactly once: the process handle is dropped as
        soon as its exit is seen.  A daemon without a process — exited, or
        never launched successfully — is launched once its back-off expires.
        """
        for name, state in self._daemon_states.items():
            proc = self._daemon_procs.get(name)
            if proc is not None:
                rc = proc.poll()
                if rc is None:
                    continue
                del self._daemon_procs[name]
                uptime = state.record_exit(exit_code=rc)
                backoff = state.schedule_restart()
                if backoff > 0:
                    logger.warning(
                        "Daemon agent '%s' (pid=%d) %s after %.0fs — %d fast failures in a row, "
                        "back-off: next attempt in %.0fs.",
                        name, proc.pid, describe_exit(rc), uptime,
                        state.consecutive_fast_failures, backoff,
                    )
                else:
                    logger.warning(
                        "Daemon agent '%s' (pid=%d) %s after %.0fs — restarting.",
                        name, proc.pid, describe_exit(rc), uptime,
                    )

            if time.monotonic() >= state.next_restart_after:
                self._launch_daemon(name)
            else:
                logger.debug(
                    "Back-off active for '%s': %.0fs remaining.",
                    name, state.next_restart_after - time.monotonic(),
                )

    # ── Scheduled management ───────────────────────────────────────────────────

    def _reap_scheduled(self) -> None:
        """Record finished scheduled runs and kill runs past their timeout."""
        for name, proc in list(self._scheduled_procs.items()):
            state = self._scheduled_states[name]
            rc = proc.poll()
            if rc is not None:
                del self._scheduled_procs[name]
                state.record_completion(rc)
                logger.log(
                    logging.INFO if rc == 0 else logging.WARNING,
                    "Scheduled agent '%s' %s. Next run in %.0fs.",
                    name, describe_exit(rc), state.config.interval_s or 0,
                )
            elif state.is_overdue():
                logger.error(
                    "Scheduled agent '%s' (pid=%d) exceeded its timeout (%.0fs) — killing.",
                    name, proc.pid, state.effective_run_timeout_s(),
                )
                proc.kill()
                try:
                    proc.wait(timeout=_KILL_REAP_TIMEOUT_S)
                except subprocess.TimeoutExpired:
                    logger.error("Scheduled agent '%s' (pid=%d) survived SIGKILL.", name, proc.pid)
                del self._scheduled_procs[name]
                state.record_completion(exit_code=-1)

    def _dispatch_scheduled(self) -> None:
        """Launch scheduled one-shot agents that are due, without waiting for them."""
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

            self._scheduled_procs[name] = proc
            state.record_dispatch(proc.pid)

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
