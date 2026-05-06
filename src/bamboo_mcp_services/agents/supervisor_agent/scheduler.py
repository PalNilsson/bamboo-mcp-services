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

"""Per-agent scheduling and daemon state tracking.

This module is intentionally free of subprocess calls so that the scheduling
logic can be unit-tested without spawning real processes.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


# ── Constants ─────────────────────────────────────────────────────────────────

#: Valid values for the ``mode`` config key.
MODE_DAEMON = "daemon"
MODE_SCHEDULED = "scheduled"

#: Minimum back-off between restart attempts (seconds).
BACKOFF_BASE_S = 5.0

#: Maximum back-off between restart attempts (seconds).
BACKOFF_MAX_S = 300.0

#: Number of rapid restarts within a window that triggers back-off escalation.
BACKOFF_RAPID_RESTART_COUNT = 3

#: Window (seconds) in which restarts are considered "rapid".
BACKOFF_RAPID_WINDOW_S = 60.0


# ── Config dataclasses ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class AgentConfig:
    """Immutable per-agent configuration parsed from YAML.

    Attributes:
        name: Unique agent identifier (used in logs and health reports).
        mode: ``"daemon"`` or ``"scheduled"``.
        command: Argument list passed verbatim to ``subprocess.Popen``.
        enabled: When ``False`` the agent is never started.
        interval_s: Seconds between scheduled one-shot runs (``mode=scheduled``
            only; ignored for daemon agents).
        depends_on_file: Optional filesystem path that must exist before this
            agent is started.  Useful to express ordering dependencies, e.g.
            waiting for ``cric.duckdb`` before starting the ingestion agent.
        depends_timeout_s: How long (seconds) to wait for ``depends_on_file``
            before starting the agent anyway with a warning.
        run_timeout_s: Maximum wall-clock time (seconds) allowed for a single
            scheduled one-shot run before the supervisor kills it.  Defaults to
            ``interval_s * 2`` when ``None``.
    """
    name: str
    mode: str
    command: list
    enabled: bool = True
    interval_s: Optional[float] = None
    depends_on_file: Optional[str] = None
    depends_timeout_s: float = 120.0
    run_timeout_s: Optional[float] = None


# ── Runtime state dataclasses ──────────────────────────────────────────────────

@dataclass
class DaemonState:
    """Mutable runtime state for a single daemon-mode agent.

    Attributes:
        config: Immutable agent configuration.
        pid: OS process ID of the currently running child, or ``None``.
        restart_count: Total number of times this agent has been restarted
            since the supervisor started.
        restart_times: Monotonic timestamps of recent restart events, used to
            detect rapid-restart loops and apply back-off.
        next_restart_after: Monotonic clock value before which the supervisor
            must not restart this agent (back-off enforcement).
        last_exit_code: Exit code of the most recently observed process exit,
            or ``None`` if the agent has never exited.
        started_at_utc: UTC timestamp of the most recent ``Popen`` call.
    """
    config: AgentConfig
    pid: Optional[int] = None
    restart_count: int = 0
    restart_times: list = field(default_factory=list)
    next_restart_after: float = 0.0
    last_exit_code: Optional[int] = None
    started_at_utc: Optional[datetime] = None

    # ── Back-off helpers ───────────────────────────────────────────────────────

    def record_exit(self, exit_code: int) -> None:
        """Record that the managed process has exited.

        Updates ``last_exit_code``, clears ``pid``, and appends the current
        monotonic time to ``restart_times`` for back-off tracking.

        Args:
            exit_code: The process exit code observed via ``proc.poll()``.
        """
        self.last_exit_code = exit_code
        self.pid = None
        now = time.monotonic()
        self.restart_times.append(now)
        # Prune timestamps outside the rapid-restart window.
        cutoff = now - BACKOFF_RAPID_WINDOW_S
        self.restart_times = [t for t in self.restart_times if t >= cutoff]

    def compute_backoff_s(self) -> float:
        """Return the number of seconds the supervisor should wait before the
        next restart attempt.

        The delay starts at ``BACKOFF_BASE_S`` and doubles for every restart
        beyond ``BACKOFF_RAPID_RESTART_COUNT`` within ``BACKOFF_RAPID_WINDOW_S``,
        capped at ``BACKOFF_MAX_S``.

        Returns:
            Delay in seconds (may be 0.0 if no back-off is warranted).
        """
        rapid = len(self.restart_times)
        if rapid <= BACKOFF_RAPID_RESTART_COUNT:
            return 0.0
        excess = rapid - BACKOFF_RAPID_RESTART_COUNT
        delay = BACKOFF_BASE_S * (2 ** (excess - 1))
        return min(delay, BACKOFF_MAX_S)

    def record_start(self, pid: int) -> None:
        """Record that a new child process has been launched.

        Args:
            pid: OS process ID of the new child.
        """
        self.pid = pid
        self.restart_count += 1
        self.started_at_utc = datetime.now(timezone.utc)

    def to_health_dict(self) -> dict[str, Any]:
        """Serialise state to a JSON-safe dictionary for health reporting.

        Returns:
            Dictionary suitable for inclusion in a ``HealthReport.details``
            mapping.
        """
        return {
            "mode": MODE_DAEMON,
            "pid": self.pid,
            "restarts": max(0, self.restart_count - 1),  # first start not a restart
            "last_exit_code": self.last_exit_code,
            "started_at_utc": (
                self.started_at_utc.isoformat() if self.started_at_utc else None
            ),
            "next_restart_after": (
                self.next_restart_after if self.next_restart_after > time.monotonic() else None
            ),
        }


@dataclass
class ScheduledState:
    """Mutable runtime state for a single scheduled (one-shot) agent.

    Attributes:
        config: Immutable agent configuration.
        next_run: Monotonic clock value at or after which the next one-shot run
            should be dispatched.
        last_run_utc: UTC timestamp of the most recent dispatch, or ``None``.
        last_exit_code: Exit code of the most recently completed run, or ``None``.
        running_pid: OS PID of the currently running one-shot process, or ``None``.
            A non-``None`` value means a previous run has not yet been reaped.
        run_count: Total number of completed runs (successful or not).
    """
    config: AgentConfig
    next_run: float = 0.0          # 0.0 ⇒ run immediately on first tick
    last_run_utc: Optional[datetime] = None
    last_exit_code: Optional[int] = None
    running_pid: Optional[int] = None
    run_count: int = 0

    def record_dispatch(self, pid: int) -> None:
        """Record that a one-shot subprocess has been launched.

        Args:
            pid: OS process ID of the launched process.
        """
        self.running_pid = pid
        self.last_run_utc = datetime.now(timezone.utc)

    def record_completion(self, exit_code: int) -> None:
        """Record that the current one-shot subprocess has completed.

        Advances ``next_run`` by ``config.interval_s`` from now.

        Args:
            exit_code: The process exit code.
        """
        self.last_exit_code = exit_code
        self.running_pid = None
        self.run_count += 1
        interval = self.config.interval_s or 0.0
        self.next_run = time.monotonic() + interval

    def is_due(self) -> bool:
        """Return ``True`` if the agent is due for its next run.

        Returns:
            ``True`` when ``time.monotonic() >= next_run`` and no run is
            currently in progress.
        """
        return self.running_pid is None and time.monotonic() >= self.next_run

    def effective_run_timeout_s(self) -> float:
        """Return the run timeout in seconds.

        Falls back to ``interval_s * 2`` when no explicit timeout is configured,
        with a floor of 60 seconds.

        Returns:
            Timeout in seconds.
        """
        if self.config.run_timeout_s is not None:
            return self.config.run_timeout_s
        if self.config.interval_s:
            return max(self.config.interval_s * 2, 60.0)
        return 300.0

    def to_health_dict(self) -> dict[str, Any]:
        """Serialise state to a JSON-safe dictionary for health reporting.

        Returns:
            Dictionary suitable for inclusion in a ``HealthReport.details``
            mapping.
        """
        now_mono = time.monotonic()
        next_dt: Optional[str] = None
        if self.running_pid is None and self.next_run > now_mono:
            delta_s = self.next_run - now_mono
            next_dt_obj = datetime.now(timezone.utc)
            # Approximate next-run wall-clock time.
            from datetime import timedelta
            next_dt = (next_dt_obj + timedelta(seconds=delta_s)).isoformat()

        return {
            "mode": MODE_SCHEDULED,
            "interval_s": self.config.interval_s,
            "last_run_utc": (
                self.last_run_utc.isoformat() if self.last_run_utc else None
            ),
            "next_run_utc": next_dt,
            "last_exit_code": self.last_exit_code,
            "running_pid": self.running_pid,
            "run_count": self.run_count,
        }
