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

"""Tests for supervisor_agent — agent.py, scheduler.py, and cli.py.

All subprocess interaction is replaced by unittest.mock so that no real
processes are spawned during the test run.
"""
from __future__ import annotations

import json
import sys
import os
import signal
import subprocess
import time
import unittest
from unittest.mock import MagicMock, patch

from bamboo_mcp_services.agents.base import AgentState
from bamboo_mcp_services.agents.supervisor_agent.agent import (
    SupervisorAgent,
    SupervisorConfig,
)
from bamboo_mcp_services.agents.supervisor_agent.scheduler import (
    AgentConfig,
    DaemonState,
    DependencyStatus,
    DependencyWait,
    ScheduledState,
    MODE_DAEMON,
    MODE_SCHEDULED,
    BACKOFF_BASE_S,
    BACKOFF_FREE_RESTARTS,
    BACKOFF_MAX_S,
    BACKOFF_MIN_UPTIME_S,
    describe_exit,
)
from bamboo_mcp_services.agents.supervisor_agent.cli import (
    _load_config,
    build_parser,
    main as supervisor_main,
)

# Capture the real Popen class at import time, before any @patch decorator
# replaces subprocess.Popen with a MagicMock.  Speccing a Mock against another
# Mock raises InvalidSpecError in Python 3.12+.
_REAL_POPEN = subprocess.Popen


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _daemon_cfg(name="test-daemon", command=None, enabled=True, depends_on_file=None, depends_timeout_s=5.0):
    return AgentConfig(
        name=name,
        mode=MODE_DAEMON,
        command=command or ["echo", name],
        enabled=enabled,
        depends_on_file=depends_on_file,
        depends_timeout_s=depends_timeout_s,
    )


def _scheduled_cfg(name="test-scheduled", command=None, interval_s=60.0, enabled=True):
    return AgentConfig(
        name=name,
        mode=MODE_SCHEDULED,
        command=command or ["echo", name],
        enabled=enabled,
        interval_s=interval_s,
    )


def _make_mock_proc(pid=1234, returncode=None):
    """Return a MagicMock that behaves like a subprocess.Popen object.

    Specs against _REAL_POPEN (the class captured at import time) rather than
    subprocess.Popen, which may have been replaced by a MagicMock by the time
    this helper is called inside a @patch-decorated test.  Speccing a Mock
    against another Mock raises InvalidSpecError in Python 3.12+.
    """
    proc = MagicMock(spec=_REAL_POPEN)
    proc.pid = pid
    proc.returncode = returncode
    proc.poll.return_value = returncode
    proc.wait.return_value = returncode
    return proc


# ─────────────────────────────────────────────────────────────────────────────
# scheduler.py — DaemonState
# ─────────────────────────────────────────────────────────────────────────────

class TestDaemonState(unittest.TestCase):

    def setUp(self):
        self.cfg = _daemon_cfg()
        self.state = DaemonState(config=self.cfg)

    def test_initial_state(self):
        self.assertIsNone(self.state.pid)
        self.assertEqual(self.state.restart_count, 0)
        self.assertIsNone(self.state.last_exit_code)

    def test_record_start(self):
        self.state.record_start(pid=42)
        self.assertEqual(self.state.pid, 42)
        self.assertEqual(self.state.restart_count, 1)
        self.assertIsNotNone(self.state.started_at_utc)

    def test_record_exit_clears_pid(self):
        self.state.record_start(pid=42)
        self.state.record_exit(exit_code=1)
        self.assertIsNone(self.state.pid)
        self.assertEqual(self.state.last_exit_code, 1)

    def _fast_exit(self):
        self.state.record_start(pid=1)
        self.state.record_exit(exit_code=1)

    def test_no_backoff_for_free_restarts(self):
        for _ in range(BACKOFF_FREE_RESTARTS):
            self._fast_exit()
        self.assertEqual(self.state.compute_backoff_s(), 0.0)

    def test_backoff_doubles_and_caps(self):
        delays = []
        for _ in range(BACKOFF_FREE_RESTARTS + 12):
            self._fast_exit()
            delays.append(self.state.compute_backoff_s())
        paid = delays[BACKOFF_FREE_RESTARTS:]
        self.assertEqual(paid[0], BACKOFF_BASE_S)
        self.assertEqual(paid[1], BACKOFF_BASE_S * 2)
        self.assertEqual(paid[2], BACKOFF_BASE_S * 4)
        self.assertEqual(paid[-1], BACKOFF_MAX_S)
        self.assertEqual(paid, sorted(paid))

    def test_backoff_engages_at_a_30s_poll_interval(self):
        """Regression: the old 3-exits-in-60s rule never fired when exits were seen 30 s apart."""
        for _ in range(BACKOFF_FREE_RESTARTS + 1):
            self.state.record_start(pid=1)
            self.state.started_monotonic = time.monotonic() - 30.0   # ran for 30 s each time
            self.state.record_exit(exit_code=1)
        self.assertGreater(self.state.compute_backoff_s(), 0.0)

    def test_long_run_resets_the_count(self):
        for _ in range(BACKOFF_FREE_RESTARTS + 3):
            self._fast_exit()
        self.state.record_start(pid=2)
        self.state.started_monotonic = time.monotonic() - BACKOFF_MIN_UPTIME_S - 1
        uptime = self.state.record_exit(exit_code=1)
        self.assertGreaterEqual(uptime, BACKOFF_MIN_UPTIME_S)
        self.assertEqual(self.state.consecutive_fast_failures, 0)
        self.assertEqual(self.state.compute_backoff_s(), 0.0)

    def test_exit_exactly_at_the_minimum_uptime_is_not_fast(self):
        # Pin the start to an integer: with a real monotonic value t,
        # (t + 60.0) - t is often 59.99999999999818, which made this test fail
        # intermittently.  1000.0 and 1060.0 are exact in binary floating point.
        self.state.record_start(pid=2)
        self.state.started_monotonic = 1000.0
        with patch("bamboo_mcp_services.agents.supervisor_agent.scheduler.time.monotonic",
                   return_value=1000.0 + BACKOFF_MIN_UPTIME_S):
            self.state.record_exit(exit_code=1)
        self.assertEqual(self.state.consecutive_fast_failures, 0)

    def test_launch_failure_counts_as_fast_failure(self):
        uptime = self.state.record_exit(exit_code=-1)   # no record_start: never launched
        self.assertEqual(uptime, 0.0)
        self.assertEqual(self.state.consecutive_fast_failures, 1)

    def test_to_health_dict_keys(self):
        self.state.record_start(pid=99)
        d = self.state.to_health_dict()
        self.assertIn("mode", d)
        self.assertIn("pid", d)
        self.assertIn("restarts", d)
        self.assertEqual(d["mode"], MODE_DAEMON)
        self.assertEqual(d["pid"], 99)


# ─────────────────────────────────────────────────────────────────────────────
# scheduler.py — ScheduledState
# ─────────────────────────────────────────────────────────────────────────────

class TestScheduledState(unittest.TestCase):

    def setUp(self):
        self.cfg = _scheduled_cfg(interval_s=60.0)
        self.state = ScheduledState(config=self.cfg)

    def test_due_immediately_on_first_tick(self):
        # next_run defaults to 0.0, so it is always in the past.
        self.assertTrue(self.state.is_due())

    def test_not_due_while_running(self):
        self.state.running_pid = 1234
        self.assertFalse(self.state.is_due())

    def test_not_due_before_interval_elapses(self):
        self.state.next_run = time.monotonic() + 9999
        self.assertFalse(self.state.is_due())

    def test_record_completion_advances_schedule(self):
        before = time.monotonic()
        self.state.record_dispatch(pid=55)
        self.state.record_completion(exit_code=0)
        self.assertIsNone(self.state.running_pid)
        self.assertEqual(self.state.last_exit_code, 0)
        self.assertEqual(self.state.run_count, 1)
        # next_run should be approximately now + interval_s.
        self.assertGreaterEqual(self.state.next_run, before + 60.0)

    def test_effective_run_timeout_default(self):
        # Default: interval_s * 2.
        self.assertEqual(self.state.effective_run_timeout_s(), 120.0)

    def test_effective_run_timeout_explicit(self):
        cfg = _scheduled_cfg(interval_s=60.0)
        cfg2 = AgentConfig(
            name=cfg.name, mode=cfg.mode, command=cfg.command,
            interval_s=cfg.interval_s, run_timeout_s=45.0,
        )
        state = ScheduledState(config=cfg2)
        self.assertEqual(state.effective_run_timeout_s(), 45.0)

    def test_to_health_dict_keys(self):
        d = self.state.to_health_dict()
        self.assertIn("mode", d)
        self.assertIn("interval_s", d)
        self.assertIn("run_count", d)
        self.assertEqual(d["mode"], MODE_SCHEDULED)


# ─────────────────────────────────────────────────────────────────────────────
# agent.py — SupervisorAgent daemon mode
# ─────────────────────────────────────────────────────────────────────────────

class TestSupervisorAgentDaemon(unittest.TestCase):

    @patch("bamboo_mcp_services.agents.supervisor_agent.agent.subprocess.Popen")
    def test_daemon_agents_started_on_start(self, mock_popen):
        mock_popen.return_value = _make_mock_proc(pid=100)

        cfg = SupervisorConfig(agents=[
            _daemon_cfg(name="alpha"),
            _daemon_cfg(name="beta"),
        ])
        sv = SupervisorAgent(config=cfg)
        sv.start()

        self.assertEqual(sv.state, AgentState.RUNNING)
        self.assertEqual(mock_popen.call_count, 2)

    @patch("bamboo_mcp_services.agents.supervisor_agent.agent.subprocess.Popen")
    def test_disabled_agent_not_started(self, mock_popen):
        cfg = SupervisorConfig(agents=[
            _daemon_cfg(name="enabled-agent"),
            _daemon_cfg(name="disabled-agent", enabled=False),
        ])
        sv = SupervisorAgent(config=cfg)
        sv.start()

        self.assertEqual(mock_popen.call_count, 1)
        started_cmds = [str(c) for c in mock_popen.call_args[0][0]]
        self.assertIn("enabled-agent", started_cmds)

    @patch("bamboo_mcp_services.agents.supervisor_agent.agent.subprocess.Popen")
    def test_daemon_restarted_on_exit(self, mock_popen):
        """When a daemon process exits, the next tick should restart it."""
        flaky_proc = _make_mock_proc(pid=200, returncode=None)
        stable_proc = _make_mock_proc(pid=201, returncode=None)
        restarted_proc = _make_mock_proc(pid=202, returncode=None)

        # start() launches flaky then stable; tick() restarts flaky after it dies.
        mock_popen.side_effect = [flaky_proc, stable_proc, restarted_proc]

        cfg = SupervisorConfig(agents=[
            _daemon_cfg(name="flaky"),
            _daemon_cfg(name="stable"),
        ])
        sv = SupervisorAgent(config=cfg)
        sv.start()

        # Simulate flaky dying: make its poll() return a non-None exit code.
        sv._daemon_procs["flaky"].poll.return_value = 1
        sv._daemon_procs["flaky"].returncode = 1

        sv.tick()

        # Popen should have been called a third time to restart the flaky agent.
        self.assertEqual(mock_popen.call_count, 3)

        sv.stop()

    @patch("bamboo_mcp_services.agents.supervisor_agent.agent.subprocess.Popen")
    def test_back_off_prevents_immediate_restart(self, mock_popen):
        """After rapid restarts, next_restart_after should block relaunch."""
        proc = _make_mock_proc(pid=300, returncode=None)
        dead_proc = _make_mock_proc(pid=301, returncode=1)
        mock_popen.side_effect = [proc, dead_proc]

        cfg = SupervisorConfig(agents=[_daemon_cfg(name="crasher")])
        sv = SupervisorAgent(config=cfg)
        sv.start()

        state = sv._daemon_states["crasher"]
        # Inject enough earlier fast failures that this exit triggers back-off.
        state.consecutive_fast_failures = BACKOFF_FREE_RESTARTS + 1

        # Simulate exit.
        sv._daemon_procs["crasher"].poll.return_value = 1
        sv.tick()

        # next_restart_after should be set in the future, and no relaunch yet.
        self.assertGreater(state.next_restart_after, time.monotonic())
        self.assertEqual(mock_popen.call_count, 1)

        sv.stop()

    @patch("bamboo_mcp_services.agents.supervisor_agent.agent.subprocess.Popen")
    def test_stop_sends_sigterm_to_children(self, mock_popen):
        proc_a = _make_mock_proc(pid=400, returncode=None)
        proc_b = _make_mock_proc(pid=401, returncode=None)
        mock_popen.side_effect = [proc_a, proc_b]

        cfg = SupervisorConfig(agents=[
            _daemon_cfg(name="svc-a"),
            _daemon_cfg(name="svc-b"),
        ])
        sv = SupervisorAgent(config=cfg)
        sv.start()
        sv.stop()

        proc_a.send_signal.assert_called_once_with(signal.SIGTERM)
        proc_b.send_signal.assert_called_once_with(signal.SIGTERM)

    @patch("bamboo_mcp_services.agents.supervisor_agent.agent.subprocess.Popen")
    def test_stop_kills_non_terminating_process(self, mock_popen):
        proc = _make_mock_proc(pid=500, returncode=None)
        # Simulate that wait() times out.
        proc.wait.side_effect = subprocess.TimeoutExpired(cmd=["echo"], timeout=1)
        mock_popen.return_value = proc

        cfg = SupervisorConfig(
            agents=[_daemon_cfg(name="stubborn")],
            stop_timeout_s=1,
        )
        sv = SupervisorAgent(config=cfg)
        sv.start()
        sv.stop()

        proc.kill.assert_called_once()

    @patch("bamboo_mcp_services.agents.supervisor_agent.agent.subprocess.Popen")
    def test_health_details_include_all_agents(self, mock_popen):
        mock_popen.return_value = _make_mock_proc(pid=600)

        cfg = SupervisorConfig(agents=[
            _daemon_cfg(name="svc-x"),
            _daemon_cfg(name="svc-y"),
        ])
        sv = SupervisorAgent(config=cfg)
        sv.start()

        report = sv.health()
        agents_detail = report.details["agents"]
        self.assertIn("svc-x", agents_detail)
        self.assertIn("svc-y", agents_detail)

        sv.stop()

    def test_unknown_mode_raises_on_start(self):
        cfg = SupervisorConfig(agents=[
            AgentConfig(name="bad", mode="invalid", command=["echo"]),
        ])
        sv = SupervisorAgent(config=cfg)
        with self.assertRaises((ValueError, Exception)):
            sv.start()


# ─────────────────────────────────────────────────────────────────────────────
# agent.py — SupervisorAgent scheduled mode
# ─────────────────────────────────────────────────────────────────────────────

class TestSupervisorAgentScheduled(unittest.TestCase):

    @patch("bamboo_mcp_services.agents.supervisor_agent.agent.subprocess.Popen")
    def test_scheduled_agent_dispatched_when_due(self, mock_popen):
        finished_proc = _make_mock_proc(pid=700, returncode=0)
        finished_proc.wait.return_value = 0
        mock_popen.return_value = finished_proc

        cfg = SupervisorConfig(agents=[_scheduled_cfg(name="hourly", interval_s=3600.0)])
        sv = SupervisorAgent(config=cfg)
        sv.start()

        # next_run defaults to 0.0, so first tick should dispatch it ...
        sv.tick()
        mock_popen.assert_called_once()
        state = sv._scheduled_states["hourly"]
        self.assertEqual(state.run_count, 0, "a tick must not wait for the run")

        # ... and the next tick reaps it.
        sv.tick()
        mock_popen.assert_called_once()
        self.assertEqual(state.run_count, 1)
        self.assertEqual(state.last_exit_code, 0)
        finished_proc.wait.assert_not_called()

    @patch("bamboo_mcp_services.agents.supervisor_agent.agent.subprocess.Popen")
    def test_scheduled_agent_not_dispatched_before_interval(self, mock_popen):
        cfg = SupervisorConfig(agents=[_scheduled_cfg(name="future", interval_s=3600.0)])
        sv = SupervisorAgent(config=cfg)
        sv.start()

        # Advance next_run far into the future.
        sv._scheduled_states["future"].next_run = time.monotonic() + 9999

        sv.tick()

        mock_popen.assert_not_called()

    @patch("bamboo_mcp_services.agents.supervisor_agent.agent.subprocess.Popen")
    def test_scheduled_agent_killed_on_timeout(self, mock_popen):
        slow_proc = _make_mock_proc(pid=800, returncode=None)
        mock_popen.return_value = slow_proc

        cfg = SupervisorConfig(agents=[
            AgentConfig(
                name="slow",
                mode=MODE_SCHEDULED,
                command=["sleep", "9999"],
                interval_s=60.0,
                run_timeout_s=1.0,
            )
        ])
        sv = SupervisorAgent(config=cfg)
        sv.start()
        sv.tick()                                  # dispatch
        state = sv._scheduled_states["slow"]
        slow_proc.kill.assert_not_called()

        state.dispatched_monotonic = time.monotonic() - 2.0   # now past run_timeout_s=1.0
        sv.tick()                                  # reap: overdue → kill

        slow_proc.kill.assert_called_once()
        self.assertEqual(state.last_exit_code, -1)
        self.assertNotIn("slow", sv._scheduled_procs)

    @patch("bamboo_mcp_services.agents.supervisor_agent.agent.subprocess.Popen")
    def test_scheduled_interval_advanced_after_completion(self, mock_popen):
        proc = _make_mock_proc(pid=900, returncode=0)
        proc.wait.return_value = 0
        mock_popen.return_value = proc

        interval = 300.0
        cfg = SupervisorConfig(agents=[_scheduled_cfg(name="periodic", interval_s=interval)])
        sv = SupervisorAgent(config=cfg)
        sv.start()
        sv.tick()   # dispatch
        sv.tick()   # reap

        state = sv._scheduled_states["periodic"]
        # next_run should now be approximately now + interval.
        self.assertGreater(state.next_run, time.monotonic())
        self.assertLessEqual(state.next_run, time.monotonic() + interval + 5)

    @patch("bamboo_mcp_services.agents.supervisor_agent.agent.subprocess.Popen")
    def test_health_details_include_scheduled_agents(self, mock_popen):
        proc = _make_mock_proc(pid=950, returncode=0)
        proc.wait.return_value = 0
        mock_popen.return_value = proc

        cfg = SupervisorConfig(agents=[_scheduled_cfg(name="job", interval_s=60.0)])
        sv = SupervisorAgent(config=cfg)
        sv.start()
        sv.tick()   # dispatch
        sv.tick()   # reap

        report = sv.health()
        agents_detail = report.details["agents"]
        self.assertIn("job", agents_detail)
        self.assertEqual(agents_detail["job"]["mode"], MODE_SCHEDULED)
        self.assertEqual(agents_detail["job"]["run_count"], 1)


# ─────────────────────────────────────────────────────────────────────────────
# agent.py — dependency waiting
# ─────────────────────────────────────────────────────────────────────────────

class TestDependencyWait(unittest.TestCase):
    """scheduler.DependencyWait: the non-blocking replacement for the old sleep loop."""

    PATH = "/data/cric.db"

    def setUp(self):
        self.wait = DependencyWait()
        self.present = False

    def _check(self, now, timeout=10.0):
        return self.wait.check(self.PATH, timeout, now, lambda p: self.present)

    def test_no_dependency_is_ready(self):
        self.assertIs(self.wait.check(None, 10.0, 0.0, lambda p: False), DependencyStatus.READY)
        self.assertIs(self.wait.check("", 10.0, 0.0, lambda p: False), DependencyStatus.READY)
        self.assertFalse(self.wait.waiting)

    def test_file_already_present_is_ready(self):
        self.present = True
        self.assertIs(self._check(100.0), DependencyStatus.READY)
        self.assertFalse(self.wait.waiting)

    def test_first_miss_starts_the_wait(self):
        self.assertIs(self._check(100.0), DependencyStatus.STARTED_WAITING)
        self.assertTrue(self.wait.waiting)
        self.assertEqual(self.wait.deadline, 110.0)

    def test_miss_before_the_deadline_keeps_waiting(self):
        self._check(100.0)
        self.assertIs(self._check(109.999), DependencyStatus.WAITING)
        self.assertTrue(self.wait.waiting)

    def test_miss_at_the_deadline_times_out(self):
        """Same boundary as the old loop, which ran while monotonic() < deadline."""
        self._check(100.0)
        self.assertIs(self._check(110.0), DependencyStatus.TIMED_OUT)
        self.assertFalse(self.wait.waiting)

    def test_file_appearing_during_the_wait_is_found(self):
        self._check(100.0)
        self.present = True
        self.assertIs(self._check(105.0), DependencyStatus.FOUND)
        self.assertFalse(self.wait.waiting)

    def test_a_new_wait_after_one_ended_gets_a_fresh_deadline(self):
        self._check(100.0)
        self._check(110.0)                  # timed out
        self.assertIs(self._check(500.0), DependencyStatus.STARTED_WAITING)
        self.assertEqual(self.wait.deadline, 510.0)

    def test_may_start(self):
        allowed = {s for s in DependencyStatus if s.may_start}
        self.assertEqual(
            allowed,
            {DependencyStatus.READY, DependencyStatus.FOUND, DependencyStatus.TIMED_OUT},
        )


class _Clock:
    """A settable stand-in for ``time.monotonic``."""

    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class TestSupervisorAgentDependency(unittest.TestCase):
    """Finding 6: waiting for a dependency file used to block the supervisor loop."""

    DEP = "/data/bamboo/db/cric.db"

    def setUp(self):
        self.existing: set = set()
        real_exists = os.path.exists
        self.clock = _Clock()
        patchers = [
            patch(
                "bamboo_mcp_services.agents.supervisor_agent.agent.os.path.exists",
                side_effect=lambda p: p in self.existing if p == self.DEP else real_exists(p),
            ),
            patch("bamboo_mcp_services.agents.supervisor_agent.agent.time.monotonic", new=self.clock),
            # Nothing in start() or tick() may sleep: that was the defect.
            patch(
                "bamboo_mcp_services.agents.supervisor_agent.agent.time.sleep",
                side_effect=AssertionError("the supervisor loop blocked"),
            ),
            patch("bamboo_mcp_services.agents.supervisor_agent.agent.subprocess.Popen"),
        ]
        mocks = [p.start() for p in patchers]
        for p in patchers:
            self.addCleanup(p.stop)
        self.popen = mocks[-1]
        self.procs: dict = {}  # agent name -> latest mock process

        def _spawn(cmd, **kwargs):
            proc = _make_mock_proc(pid=2000 + self.popen.call_count)
            self.procs[cmd[1]] = proc
            return proc
        self.popen.side_effect = _spawn

    def _launched(self):
        return [c.args[0][1] for c in self.popen.call_args_list]

    def test_dependency_file_already_exists(self):
        self.existing.add(self.DEP)
        sv = SupervisorAgent(config=SupervisorConfig(agents=[
            _daemon_cfg(name="dependent", depends_on_file=self.DEP),
        ]))
        sv.start()
        self.assertEqual(self._launched(), ["dependent"])

    def test_start_returns_at_once_and_launches_when_the_file_appears(self):
        sv = SupervisorAgent(config=SupervisorConfig(agents=[
            _daemon_cfg(name="waiter", depends_on_file=self.DEP, depends_timeout_s=120.0),
        ]))
        sv.start()
        self.assertEqual(self._launched(), [])
        self.assertEqual(sv.health().details["agents"]["waiter"]["waiting_for"], self.DEP)

        self.clock.now += 30
        sv.tick()
        self.assertEqual(self._launched(), [])

        self.existing.add(self.DEP)
        self.clock.now += 30
        with self.assertLogs("bamboo_mcp_services.agents.supervisor_agent.agent", level="INFO") as logs:
            sv.tick()
        self.assertEqual(self._launched(), ["waiter"])
        self.assertTrue(any("found — starting agent 'waiter'" in m for m in logs.output))
        self.assertIsNone(sv.health().details["agents"]["waiter"]["waiting_for"])

    def test_timeout_starts_the_agent_anyway(self):
        sv = SupervisorAgent(config=SupervisorConfig(agents=[
            _daemon_cfg(name="impatient", depends_on_file=self.DEP, depends_timeout_s=60.0),
        ]))
        sv.start()
        self.clock.now += 59.9
        sv.tick()
        self.assertEqual(self._launched(), [])

        self.clock.now += 0.1
        with self.assertLogs("bamboo_mcp_services.agents.supervisor_agent.agent", level="WARNING") as logs:
            sv.tick()
        self.assertEqual(self._launched(), ["impatient"])
        self.assertTrue(any("starting agent 'impatient' anyway" in m for m in logs.output))

    def test_a_waiting_daemon_does_not_hold_up_the_others(self):
        """The defect itself: one wait stopped every other agent being handled."""
        sv = SupervisorAgent(config=SupervisorConfig(agents=[
            _daemon_cfg(name="waiter", depends_on_file=self.DEP, depends_timeout_s=600.0),
            _daemon_cfg(name="other"),
            _scheduled_cfg(name="job", interval_s=600.0),
        ]))
        sv.start()
        self.assertEqual(self._launched(), ["other"])
        sv.tick()
        self.assertEqual(self._launched(), ["other", "job"])

        # "other" exits; it is restarted on the next tick while "waiter" still waits.
        self.procs["other"].poll.return_value = 1
        self.clock.now += 120
        sv.tick()
        self.assertEqual(self._launched(), ["other", "job", "other"])

    def test_a_restart_does_not_wait_again(self):
        """Only a daemon's first launch waits, as before the change."""
        self.existing.add(self.DEP)
        sv = SupervisorAgent(config=SupervisorConfig(agents=[
            _daemon_cfg(name="dependent", depends_on_file=self.DEP),
        ]))
        sv.start()
        self.existing.clear()
        self.procs["dependent"].poll.return_value = 1
        self.clock.now += 120
        sv.tick()
        self.assertEqual(self._launched(), ["dependent", "dependent"])

    def test_scheduled_agent_waits_without_holding_up_the_others(self):
        sv = SupervisorAgent(config=SupervisorConfig(agents=[
            AgentConfig(
                name="dependent-job", mode=MODE_SCHEDULED, command=["echo", "dependent-job"],
                interval_s=600.0, depends_on_file=self.DEP, depends_timeout_s=300.0,
            ),
            _scheduled_cfg(name="free-job", interval_s=600.0),
        ]))
        sv.start()
        sv.tick()
        self.assertEqual(self._launched(), ["free-job"])
        self.assertEqual(sv.health().details["agents"]["dependent-job"]["waiting_for"], self.DEP)

        self.existing.add(self.DEP)
        self.clock.now += 30
        sv.tick()
        self.assertEqual(self._launched(), ["free-job", "dependent-job"])


# ─────────────────────────────────────────────────────────────────────────────
# agent.py — once-tick mode
# ─────────────────────────────────────────────────────────────────────────────

class TestSupervisorAgentOnceTick(unittest.TestCase):

    @patch("bamboo_mcp_services.agents.supervisor_agent.agent.subprocess.Popen")
    def test_once_tick_does_not_restart_already_running_daemon(self, mock_popen):
        """A single tick should not restart a daemon that is still running."""
        proc = _make_mock_proc(pid=1100, returncode=None)
        mock_popen.return_value = proc

        cfg = SupervisorConfig(agents=[_daemon_cfg(name="steady")])
        sv = SupervisorAgent(config=cfg)
        sv.start()

        initial_call_count = mock_popen.call_count
        sv.tick()

        self.assertEqual(mock_popen.call_count, initial_call_count)
        sv.stop()

    @patch("bamboo_mcp_services.agents.supervisor_agent.agent.subprocess.Popen")
    def test_start_stop_cycle_cleans_up(self, mock_popen):
        proc = _make_mock_proc(pid=1200, returncode=None)
        mock_popen.return_value = proc

        cfg = SupervisorConfig(agents=[_daemon_cfg(name="lifecycle")])
        sv = SupervisorAgent(config=cfg)
        sv.start()
        self.assertEqual(sv.state, AgentState.RUNNING)
        sv.stop()
        self.assertEqual(sv.state, AgentState.STOPPED)


# ─────────────────────────────────────────────────────────────────────────────
# cli.py — config loading
# ─────────────────────────────────────────────────────────────────────────────

class TestCLIConfigLoading(unittest.TestCase):

    def _write_config(self, tmp_path, content):
        path = os.path.join(tmp_path, "supervisor-agent.yaml")
        with open(path, "w") as fh:
            fh.write(content)
        return path

    def test_load_valid_config(self, tmp_path=None):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_config(tmp, """
health_poll_interval_s: 45
agents:
  - name: alpha
    mode: daemon
    command: [bamboo-cric, --data, cric.duckdb]
  - name: beta
    mode: scheduled
    interval_s: 300
    command: [bamboo-github-sync, --once]
""")
            cfg = _load_config(path)

        self.assertIsNotNone(cfg)
        self.assertEqual(cfg.health_poll_interval_s, 45.0)
        self.assertEqual(len(cfg.agents), 2)

        names = [a.name for a in cfg.agents]
        self.assertIn("alpha", names)
        self.assertIn("beta", names)

        beta = next(a for a in cfg.agents if a.name == "beta")
        self.assertEqual(beta.mode, MODE_SCHEDULED)
        self.assertEqual(beta.interval_s, 300.0)

    def test_load_missing_file_returns_none(self):
        result = _load_config("/does/not/exist/supervisor.yaml")
        self.assertIsNone(result)

    def test_load_agent_with_unknown_mode_skipped(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_config(tmp, """
agents:
  - name: good
    mode: daemon
    command: [echo, good]
  - name: bad
    mode: teleport
    command: [echo, bad]
""")
            cfg = _load_config(path)

        self.assertIsNotNone(cfg)
        names = [a.name for a in cfg.agents]
        self.assertIn("good", names)
        self.assertNotIn("bad", names)

    def test_load_scheduled_without_interval_skipped(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_config(tmp, """
agents:
  - name: no-interval
    mode: scheduled
    command: [echo, hi]
""")
            cfg = _load_config(path)

        self.assertIsNotNone(cfg)
        self.assertEqual(len(cfg.agents), 0)

    def test_load_agent_without_command_skipped(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_config(tmp, """
agents:
  - name: no-cmd
    mode: daemon
""")
            cfg = _load_config(path)

        self.assertIsNotNone(cfg)
        self.assertEqual(len(cfg.agents), 0)

    def test_load_disabled_agent_parsed_correctly(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_config(tmp, """
agents:
  - name: "off"
    mode: daemon
    enabled: false
    command: [echo, "off"]
""")
            cfg = _load_config(path)

        self.assertIsNotNone(cfg)
        self.assertEqual(len(cfg.agents), 1)
        self.assertFalse(cfg.agents[0].enabled)

    def test_build_parser_defaults(self):
        parser = build_parser()
        args = parser.parse_args([])
        self.assertFalse(args.once)
        self.assertFalse(args.status)
        self.assertEqual(args.log_level, "INFO")


# ─────────────────────────────────────────────────────────────────────────────
# cli.py — --status flag
# ─────────────────────────────────────────────────────────────────────────────

class TestCLIStatus(unittest.TestCase):
    """--status reads the heartbeat file (S-18); it used to echo the config."""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "heartbeat.json")

    def tearDown(self):
        self._tmp.cleanup()

    def _status(self, *extra):
        from io import StringIO
        from contextlib import redirect_stdout
        out = StringIO()
        with redirect_stdout(out):
            rc = supervisor_main(["--status", "--heartbeat-file", self.path, *extra])
        return rc, json.loads(out.getvalue())

    def _write(self, state="running", age_s=0.0):
        from bamboo_mcp_services.agents.supervisor_agent.heartbeat import build_heartbeat
        doc = build_heartbeat({"name": "supervisor-agent", "state": state, "details": {}},
                              pid=4242, now=time.time() - age_s)
        with open(self.path, "w") as fh:
            json.dump(doc, fh)

    def test_fresh_heartbeat_exits_0(self):
        self._write()
        rc, out = self._status()
        self.assertEqual(rc, 0)
        self.assertTrue(out["ok"])
        self.assertEqual(out["heartbeat"]["supervisor_pid"], 4242)

    def test_stale_heartbeat_exits_1(self):
        self._write(age_s=300)
        rc, out = self._status("--max-age", "120")
        self.assertEqual(rc, 1)
        self.assertIn("stale", out["reason"])

    def test_missing_heartbeat_exits_1(self):
        rc, out = self._status()
        self.assertEqual(rc, 1)
        self.assertIn("no heartbeat file", out["reason"])

    def test_no_heartbeat_file_configured_exits_1(self):
        from io import StringIO
        from contextlib import redirect_stdout
        with redirect_stdout(StringIO()):
            self.assertEqual(supervisor_main(["--status", "--heartbeat-file", ""]), 1)

    def test_status_writes_no_log_and_reads_no_config(self):
        """A probe runs --status every few seconds; it must not touch the log file."""
        self._write()
        log = os.path.join(self._tmp.name, "supervisor.log")
        rc, _ = self._status("--log-file", log, "--config", "/nonexistent/supervisor.yaml")
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(log))


if __name__ == "__main__":
    unittest.main()


# ─────────────────────────────────────────────────────────────────────────────
# Regressions found on aipanda033 / in the sandbox (S-5, findings 1–3, 8, 9)
# ─────────────────────────────────────────────────────────────────────────────

class TestLaunchFailureIsRetried(unittest.TestCase):
    """Finding 1: a daemon whose launch failed was never tried again."""

    @patch("bamboo_mcp_services.agents.supervisor_agent.agent.subprocess.Popen")
    def test_failed_launch_is_retried_on_the_next_tick(self, mock_popen):
        proc = _make_mock_proc(pid=1300)
        mock_popen.side_effect = [FileNotFoundError("bamboo-cric"), proc]

        sv = SupervisorAgent(config=SupervisorConfig(agents=[_daemon_cfg(name="cric")]))
        sv.start()
        self.assertEqual(sv.daemons_not_running(), ["cric"])

        sv.tick()

        self.assertEqual(mock_popen.call_count, 2)
        self.assertEqual(sv.daemons_not_running(), [])
        self.assertEqual(sv._daemon_states["cric"].pid, 1300)
        sv.stop()

    @patch("bamboo_mcp_services.agents.supervisor_agent.agent.subprocess.Popen")
    def test_repeated_launch_failures_back_off(self, mock_popen):
        mock_popen.side_effect = FileNotFoundError("bamboo-cric")
        sv = SupervisorAgent(config=SupervisorConfig(agents=[_daemon_cfg(name="cric")]))
        sv.start()                                   # failure 1: free, retry at next tick
        sv.tick()                                    # failure 2: back-off starts
        state = sv._daemon_states["cric"]
        self.assertGreater(state.next_restart_after, time.monotonic())
        calls = mock_popen.call_count
        sv.tick()                                    # inside back-off: no attempt
        self.assertEqual(mock_popen.call_count, calls)


class TestExitIsRecordedOnce(unittest.TestCase):
    """Finding 3: during back-off every tick re-recorded the same exit, re-arming 300 s forever."""

    @patch("bamboo_mcp_services.agents.supervisor_agent.agent.subprocess.Popen")
    def test_back_off_is_not_re_armed_by_later_ticks(self, mock_popen):
        first = _make_mock_proc(pid=1400, returncode=None)
        second = _make_mock_proc(pid=1401, returncode=None)
        mock_popen.side_effect = [first, second]
        sv = SupervisorAgent(config=SupervisorConfig(agents=[_daemon_cfg(name="crasher")]))
        sv.start()
        state = sv._daemon_states["crasher"]
        state.consecutive_fast_failures = BACKOFF_FREE_RESTARTS   # this exit will incur back-off

        first.poll.return_value = 3
        sv.tick()
        failures = state.consecutive_fast_failures
        restart_after = state.next_restart_after

        for _ in range(5):
            sv.tick()

        self.assertEqual(state.consecutive_fast_failures, failures, "the same exit was counted again")
        self.assertEqual(state.next_restart_after, restart_after, "back-off was re-armed without a new exit")
        self.assertEqual(mock_popen.call_count, 1)

        state.next_restart_after = time.monotonic() - 1     # back-off expires
        sv.tick()
        self.assertEqual(mock_popen.call_count, 2)
        sv.stop()


class TestScheduledRunsDoNotBlock(unittest.TestCase):
    """Finding 2: a scheduled run blocked the tick, so daemons went unmonitored."""

    @patch("bamboo_mcp_services.agents.supervisor_agent.agent.subprocess.Popen")
    def test_daemon_exit_is_handled_while_a_scheduled_run_is_in_flight(self, mock_popen):
        daemon_1 = _make_mock_proc(pid=1500)
        sched = _make_mock_proc(pid=1501, returncode=None)
        daemon_2 = _make_mock_proc(pid=1502)
        mock_popen.side_effect = [daemon_1, sched, daemon_2]
        sv = SupervisorAgent(config=SupervisorConfig(agents=[
            _daemon_cfg(name="ingestion"),
            _scheduled_cfg(name="github-sync", interval_s=3600.0),
        ]))
        sv.start()
        sv.tick()                                  # dispatches github-sync, which keeps running
        sched.wait.assert_not_called()

        daemon_1.poll.return_value = 1             # ingestion dies mid-sync
        sv.tick()

        self.assertEqual(mock_popen.call_count, 3, "ingestion was not restarted while github-sync ran")
        self.assertIn("github-sync", sv._scheduled_procs)
        sv.stop()

    @patch("bamboo_mcp_services.agents.supervisor_agent.agent.subprocess.Popen")
    def test_stop_terminates_an_in_flight_scheduled_run(self, mock_popen):
        sched = _make_mock_proc(pid=1600, returncode=None)
        mock_popen.return_value = sched
        sv = SupervisorAgent(config=SupervisorConfig(agents=[_scheduled_cfg(name="sync")]))
        sv.start()
        sv.tick()
        sv.stop()
        sched.send_signal.assert_called_once_with(signal.SIGTERM)

    @patch("bamboo_mcp_services.agents.supervisor_agent.agent.subprocess.Popen")
    def test_children_still_running_at_the_deadline_are_all_killed(self, mock_popen):
        """The old wait loop gave up at the deadline and left later children alive."""
        procs = [_make_mock_proc(pid=1700 + i) for i in range(3)]
        for p in procs:
            p.wait.side_effect = [subprocess.TimeoutExpired(cmd=["x"], timeout=0), None]
        mock_popen.side_effect = procs
        sv = SupervisorAgent(config=SupervisorConfig(
            agents=[_daemon_cfg(name=f"d{i}") for i in range(3)], stop_timeout_s=0.0,
        ))
        sv.start()
        sv.stop()
        for p in procs:
            p.kill.assert_called_once()


class TestExitDescriptions(unittest.TestCase):
    """Finding 9: rc=-15 was logged at INFO next to 'stopped cleanly'."""

    def test_describe_exit(self):
        self.assertEqual(describe_exit(0), "exited with code 0")
        self.assertEqual(describe_exit(3), "exited with code 3")
        self.assertEqual(describe_exit(-signal.SIGTERM), "killed by SIGTERM")
        self.assertEqual(describe_exit(-signal.SIGKILL), "killed by SIGKILL")
        self.assertEqual(describe_exit(None), "still running")
        self.assertEqual(describe_exit(-999), "killed by signal 999")

    @patch("bamboo_mcp_services.agents.supervisor_agent.agent.subprocess.Popen")
    def test_child_killed_by_sigterm_is_a_warning(self, mock_popen):
        proc = _make_mock_proc(pid=1800)
        proc.returncode = -signal.SIGTERM
        mock_popen.return_value = proc
        sv = SupervisorAgent(config=SupervisorConfig(agents=[_daemon_cfg(name="cric")]))
        sv.start()
        with self.assertLogs("bamboo_mcp_services.agents.supervisor_agent.agent", level="WARNING") as logs:
            sv.stop()
        self.assertTrue(any("killed by SIGTERM" in m for m in logs.output), logs.output)


# ─────────────────────────────────────────────────────────────────────────────
# Real processes — the defects above were invisible to mock-only tests
# ─────────────────────────────────────────────────────────────────────────────

def _write_config(tmp_dir: str, body: str) -> str:
    path = os.path.join(tmp_dir, "supervisor.yaml")
    with open(path, "w") as fh:
        fh.write(body)
    return path


class TestRealProcesses(unittest.TestCase):

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_tick_returns_while_a_scheduled_run_is_still_going(self):
        sv = SupervisorAgent(config=SupervisorConfig(agents=[
            AgentConfig(name="slow", mode=MODE_SCHEDULED, command=["sleep", "5"], interval_s=60.0),
        ], stop_timeout_s=5))
        sv.start()
        t0 = time.monotonic()
        sv.tick()
        self.assertLess(time.monotonic() - t0, 2.0, "tick waited for the scheduled run")
        sv.stop()

    def test_once_gives_daemons_time_and_passes(self):
        """Finding 8: --once used to SIGTERM daemons milliseconds after launching them."""
        marker = os.path.join(self.tmp, "daemon-ran")
        cfg = _write_config(self.tmp, f"""
agents:
  - name: daemon
    mode: daemon
    command: [sh, -c, "touch {marker}; exec sleep 60"]
  - name: job
    mode: scheduled
    interval_s: 60
    command: [sh, -c, "exit 0"]
""")
        rc = supervisor_main(["-c", cfg, "--once", "--once-settle-s", "1", "--log-file", ""])
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(marker), "daemon was stopped before it ran")

    def test_once_fails_when_a_daemon_dies(self):
        cfg = _write_config(self.tmp, """
agents:
  - name: crasher
    mode: daemon
    command: [sh, -c, "exit 3"]
""")
        rc = supervisor_main(["-c", cfg, "--once", "--once-settle-s", "0.5", "--log-file", ""])
        self.assertEqual(rc, 1)

    def test_once_fails_when_a_scheduled_run_fails(self):
        cfg = _write_config(self.tmp, """
agents:
  - name: job
    mode: scheduled
    interval_s: 60
    command: [sh, -c, "exit 2"]
""")
        rc = supervisor_main(["-c", cfg, "--once", "--once-settle-s", "0", "--log-file", ""])
        self.assertEqual(rc, 1)

    def test_once_waits_for_the_scheduled_run(self):
        out = os.path.join(self.tmp, "ran")
        cfg = _write_config(self.tmp, f"""
agents:
  - name: job
    mode: scheduled
    interval_s: 60
    command: [sh, -c, "sleep 1; touch {out}"]
""")
        rc = supervisor_main(["-c", cfg, "--once", "--log-file", ""])
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(out), "--once stopped before the scheduled run finished")

    def test_once_waits_for_a_daemon_dependency(self):
        """--once still gives a dependent daemon its chance, as the blocking wait did."""
        dep = os.path.join(self.tmp, "cric.db")
        marker = os.path.join(self.tmp, "dependent-ran")
        cfg = _write_config(self.tmp, f"""
agents:
  - name: producer
    mode: daemon
    command: [sh, -c, "sleep 1; touch {dep}; exec sleep 60"]
  - name: dependent
    mode: daemon
    depends_on_file: {dep}
    depends_timeout_s: 30
    command: [sh, -c, "touch {marker}; exec sleep 60"]
""")
        rc = supervisor_main(["-c", cfg, "--once", "--once-settle-s", "1", "--log-file", ""])
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(marker), "the dependent daemon never ran")

    def test_once_dispatches_a_waiting_scheduled_agent_and_runs_nothing_twice(self):
        """--once waits for a dependent scheduled run, without re-running agents that are due again.

        The dependency appears only after quick-job has finished and been
        reaped, so --once must keep waiting with no run in flight.
        """
        dep = os.path.join(self.tmp, "ready")
        runs = os.path.join(self.tmp, "runs")
        cfg = _write_config(self.tmp, f"""
agents:
  - name: producer
    mode: daemon
    command: [sh, -c, "sleep 3; touch {dep}; exec sleep 60"]
  - name: dependent-job
    mode: scheduled
    interval_s: 60
    depends_on_file: {dep}
    depends_timeout_s: 30
    command: [sh, -c, "echo dependent >> {runs}"]
  - name: quick-job
    mode: scheduled
    interval_s: 0.01
    command: [sh, -c, "echo quick >> {runs}"]
""")
        rc = supervisor_main(["-c", cfg, "--once", "--once-settle-s", "0", "--log-file", ""])
        self.assertEqual(rc, 0)
        with open(runs) as fh:
            self.assertEqual(sorted(fh.read().split()), ["dependent", "quick"])

    def test_missing_command_is_retried(self):
        sv = SupervisorAgent(config=SupervisorConfig(agents=[
            AgentConfig(name="missing", mode=MODE_DAEMON, command=["/nonexistent/bamboo-cric"]),
        ]))
        sv.start()
        with self.assertLogs("bamboo_mcp_services.agents.supervisor_agent.agent", level="ERROR") as logs:
            sv.tick()
        self.assertTrue(any("Failed to launch daemon agent 'missing'" in m for m in logs.output))
        sv.stop()


# ─────────────────────────────────────────────────────────────────────────────
# S-8: terminal signals reach only the supervisor; children die with it
# ─────────────────────────────────────────────────────────────────────────────

_CHILD = """
import pathlib, signal, sys, time
out = pathlib.Path(sys.argv[1])
def _on(signum, frame):
    out.write_text(signal.Signals(signum).name)
    sys.exit(0)
signal.signal(signal.SIGTERM, _on)
signal.signal(signal.SIGINT, _on)
pathlib.Path(sys.argv[1] + ".started").write_text("x")
while True:
    time.sleep(0.05)
"""


class TestChildSessions(unittest.TestCase):
    """A Ctrl-C used to reach the children directly, racing the supervisor's SIGTERM."""

    @patch("bamboo_mcp_services.agents.supervisor_agent.agent.subprocess.Popen")
    def test_children_get_their_own_session(self, mock_popen):
        mock_popen.return_value = _make_mock_proc(pid=1900)
        sv = SupervisorAgent(config=SupervisorConfig(agents=[_daemon_cfg(name="cric")]))
        sv.start()
        self.assertIs(mock_popen.call_args.kwargs.get("start_new_session"), True)
        sv.stop()

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.child_script = os.path.join(self.tmp, "child.py")
        with open(self.child_script, "w") as fh:
            fh.write(_CHILD)
        self.log = os.path.join(self.tmp, "supervisor.log")
        self.heartbeat = os.path.join(self.tmp, "heartbeat.json")

    def tearDown(self):
        self._tmp.cleanup()

    def _start_supervisor(self, n_children: int = 2):
        import sys
        outs = [os.path.join(self.tmp, f"child{i}.signal") for i in range(n_children)]
        agents = "\n".join(
            f"  - name: child{i}\n    mode: daemon\n    command: [{sys.executable!r}, {self.child_script!r}, {out!r}]"
            for i, out in enumerate(outs)
        )
        cfg = _write_config(self.tmp, f"health_poll_interval_s: 1\nstop_timeout_s: 5\nagents:\n{agents}\n")
        code = (
            "from bamboo_mcp_services.agents.supervisor_agent.cli import main; "
            f"raise SystemExit(main(['-c', {cfg!r}, '--log-file', {self.log!r}, "
            f"'--heartbeat-file', {self.heartbeat!r}]))"
        )
        # Own session, so the test can signal the supervisor's process group the
        # way a terminal does on Ctrl-C, without signalling the test runner.
        sup = subprocess.Popen([sys.executable, "-c", code], start_new_session=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 20
        while not all(os.path.exists(o + ".started") for o in outs):
            self.assertLess(time.monotonic(), deadline, "children did not start")
            time.sleep(0.05)
        return sup, outs

    def _read(self, path):
        with open(path) as fh:
            return fh.read()

    def test_ctrl_c_on_the_process_group_stops_children_cleanly(self):
        sup, outs = self._start_supervisor()
        os.killpg(sup.pid, signal.SIGINT)           # what the terminal does on Ctrl-C
        self.assertEqual(sup.wait(timeout=20), 0)
        for out in outs:
            self.assertEqual(self._read(out), "SIGTERM", "child got the terminal's SIGINT, not the supervisor's SIGTERM")
        log = self._read(self.log)
        self.assertNotIn("killed by", log)
        self.assertEqual(log.count("exited with code 0"), len(outs))

    def test_sighup_is_an_orderly_stop(self):
        sup, outs = self._start_supervisor(n_children=1)
        os.killpg(sup.pid, signal.SIGHUP)           # tmux pane / terminal closed
        self.assertEqual(sup.wait(timeout=20), 0)
        self.assertEqual(self._read(outs[0]), "SIGTERM")
        self.assertIn("exited with code 0", self._read(self.log))

    def test_heartbeat_tracks_the_running_supervisor_and_records_the_stop(self):
        """S-18 end to end: a real supervisor writes the file that --status reads."""
        from bamboo_mcp_services.agents.supervisor_agent.heartbeat import check_heartbeat

        def _written_at():
            beat = check_heartbeat(self.heartbeat, max_age_s=10).heartbeat
            return None if beat is None else beat["written_at_epoch"]

        sup, _ = self._start_supervisor(n_children=1)
        try:
            deadline = time.monotonic() + 10
            while not os.path.exists(self.heartbeat):
                self.assertLess(time.monotonic(), deadline, "no heartbeat written after start")
                time.sleep(0.05)
            result = check_heartbeat(self.heartbeat, max_age_s=10)
            self.assertTrue(result.ok, result.reason)
            assert result.heartbeat is not None
            self.assertEqual(result.heartbeat["supervisor_pid"], sup.pid)
            self.assertIn("child0", result.heartbeat["health"]["details"]["agents"])
            first = result.heartbeat["written_at_epoch"]
            deadline = time.monotonic() + 10        # health_poll_interval_s is 1
            while _written_at() == first:
                self.assertLess(time.monotonic(), deadline, "heartbeat not rewritten by the loop")
                time.sleep(0.1)
        finally:
            os.killpg(sup.pid, signal.SIGTERM)
            self.assertEqual(sup.wait(timeout=20), 0)
        stopped = check_heartbeat(self.heartbeat, max_age_s=10)
        self.assertFalse(stopped.ok)
        self.assertIn("'stopped'", stopped.reason)

    @unittest.skipUnless(sys.platform.startswith("linux"), "PR_SET_PDEATHSIG is Linux-only")
    def test_children_die_when_the_supervisor_is_killed(self):
        sup, outs = self._start_supervisor()
        os.kill(sup.pid, signal.SIGKILL)            # no cleanup possible in the supervisor
        sup.wait(timeout=10)
        deadline = time.monotonic() + 10
        while not all(os.path.exists(o) for o in outs):
            self.assertLess(time.monotonic(), deadline, "a child outlived its SIGKILLed supervisor")
            time.sleep(0.05)
        for out in outs:
            self.assertEqual(self._read(out), "SIGTERM")
