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

Restart back-off
----------------
A daemon exit counts as a *fast failure* when the process had been up for
less than :data:`BACKOFF_MIN_UPTIME_S`, or never started at all (a launch
failure).  The first :data:`BACKOFF_FREE_RESTARTS` consecutive fast failures
are restarted at the next tick; after that each further one doubles the delay,
from :data:`BACKOFF_BASE_S` up to :data:`BACKOFF_MAX_S`.  An exit after a run
longer than the minimum uptime resets the count.

Counting consecutive short runs — rather than exits inside a time window, as
earlier versions did — is what lets back-off engage at the default 30 s poll
interval: a window of 60 s can never hold more than two or three exits
observed 30 s apart, so a threshold above that was never reached.
"""
from __future__ import annotations

import signal
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional


# ── Constants ─────────────────────────────────────────────────────────────────

#: Valid values for the ``mode`` config key.
MODE_DAEMON = "daemon"
MODE_SCHEDULED = "scheduled"

#: A daemon that exits after running less than this (seconds) is a fast failure.
BACKOFF_MIN_UPTIME_S = 60.0

#: Consecutive fast failures that are restarted without any delay.
BACKOFF_FREE_RESTARTS = 1

#: Delay (seconds) after the first fast failure that is not free.
BACKOFF_BASE_S = 5.0

#: Maximum delay (seconds) between restart attempts.
BACKOFF_MAX_S = 300.0


def describe_exit(returncode: Optional[int]) -> str:
    """Describe a ``Popen.returncode`` in words an operator can act on.

    A negative return code means the process was killed by that signal rather
    than exiting by itself; reporting it as ``rc=-15`` hides the difference
    between a clean shutdown and a process that never got to handle SIGTERM.

    Args:
        returncode: ``Popen.returncode`` — ``None`` while running, negative
            when killed by a signal.

    Returns:
        For example ``"exited with code 0"`` or ``"killed by SIGTERM"``.
    """
    if returncode is None:
        return "still running"
    if returncode < 0:
        try:
            return f"killed by {signal.Signals(-returncode).name}"
        except ValueError:
            return f"killed by signal {-returncode}"
    return f"exited with code {returncode}"


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
        restart_count: Number of successful launches since the supervisor
            started (the first launch included).
        consecutive_fast_failures: Exits in a row after less than
            :data:`BACKOFF_MIN_UPTIME_S` of uptime, launch failures included.
        next_restart_after: Monotonic clock value before which the supervisor
            must not restart this agent (back-off enforcement).
        last_exit_code: Exit code of the most recently observed process exit,
            ``-1`` for a launch failure, or ``None`` if the agent has never
            exited.
        started_at_utc: UTC timestamp of the most recent ``Popen`` call.
        started_monotonic: Monotonic time of the most recent launch, or
            ``None`` when no process is running.
    """
    config: AgentConfig
    pid: Optional[int] = None
    restart_count: int = 0
    consecutive_fast_failures: int = 0
    next_restart_after: float = 0.0
    last_exit_code: Optional[int] = None
    started_at_utc: Optional[datetime] = None
    started_monotonic: Optional[float] = None

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def record_start(self, pid: int) -> None:
        """Record that a new child process has been launched.

        Args:
            pid: OS process ID of the new child.
        """
        self.pid = pid
        self.restart_count += 1
        self.started_at_utc = datetime.now(timezone.utc)
        self.started_monotonic = time.monotonic()

    def record_exit(self, exit_code: int) -> float:
        """Record that the managed process has exited, or failed to launch.

        Updates the fast-failure count: an exit after less than
        :data:`BACKOFF_MIN_UPTIME_S` (or with no recorded start, i.e. a launch
        failure) increments it; a longer run resets it to zero.

        Call this exactly once per exit.  Calling it again for the same exit
        would count a failure that did not happen.

        Args:
            exit_code: The process exit code, or ``-1`` for a launch failure.

        Returns:
            How long the process had been running, in seconds (``0.0`` for a
            launch failure).
        """
        uptime = 0.0
        if self.started_monotonic is not None:
            uptime = max(0.0, time.monotonic() - self.started_monotonic)
        self.last_exit_code = exit_code
        self.pid = None
        self.started_monotonic = None
        if uptime < BACKOFF_MIN_UPTIME_S:
            self.consecutive_fast_failures += 1
        else:
            self.consecutive_fast_failures = 0
        return uptime

    def compute_backoff_s(self) -> float:
        """Return the delay before the next restart attempt.

        Returns:
            ``0.0`` for up to :data:`BACKOFF_FREE_RESTARTS` consecutive fast
            failures; otherwise :data:`BACKOFF_BASE_S` doubled for each further
            one, capped at :data:`BACKOFF_MAX_S`.
        """
        excess = self.consecutive_fast_failures - BACKOFF_FREE_RESTARTS
        if excess <= 0:
            return 0.0
        return min(BACKOFF_BASE_S * (2 ** (excess - 1)), BACKOFF_MAX_S)

    def schedule_restart(self) -> float:
        """Set ``next_restart_after`` from the current back-off.

        Returns:
            The back-off applied, in seconds.
        """
        backoff = self.compute_backoff_s()
        self.next_restart_after = time.monotonic() + backoff
        return backoff

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
            "consecutive_fast_failures": self.consecutive_fast_failures,
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
        dispatched_monotonic: Monotonic time of the current run's dispatch,
            or ``None`` when no run is in progress.
    """
    config: AgentConfig
    next_run: float = 0.0          # 0.0 ⇒ run immediately on first tick
    last_run_utc: Optional[datetime] = None
    last_exit_code: Optional[int] = None
    running_pid: Optional[int] = None
    run_count: int = 0
    dispatched_monotonic: Optional[float] = None

    def record_dispatch(self, pid: int) -> None:
        """Record that a one-shot subprocess has been launched.

        Args:
            pid: OS process ID of the launched process.
        """
        self.running_pid = pid
        self.last_run_utc = datetime.now(timezone.utc)
        self.dispatched_monotonic = time.monotonic()

    def record_completion(self, exit_code: int) -> None:
        """Record that the current one-shot subprocess has completed.

        Advances ``next_run`` by ``config.interval_s`` from now.

        Args:
            exit_code: The process exit code.
        """
        self.last_exit_code = exit_code
        self.running_pid = None
        self.dispatched_monotonic = None
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

    def is_overdue(self) -> bool:
        """Return ``True`` if the current run has exceeded its timeout.

        Returns:
            ``True`` when a run is in progress and has lasted at least
            :meth:`effective_run_timeout_s` seconds.
        """
        if self.dispatched_monotonic is None:
            return False
        return time.monotonic() - self.dispatched_monotonic >= self.effective_run_timeout_s()

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
            next_dt = (datetime.now(timezone.utc) + timedelta(seconds=delta_s)).isoformat()

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
