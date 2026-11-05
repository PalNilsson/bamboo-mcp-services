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
        self.state.record_start(pid=2)
        started = self.state.started_monotonic
        assert started is not None
        with patch("bamboo_mcp_services.agents.supervisor_agent.scheduler.time.monotonic",
                   return_value=started + BACKOFF_MIN_UPTIME_S):
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

class TestSupervisorAgentDependency(unittest.TestCase):

    @patch("bamboo_mcp_services.agents.supervisor_agent.agent.subprocess.Popen")
    def test_dependency_file_already_exists(self, mock_popen):
        """Agent with a depends_on_file that already exists should start immediately."""
        mock_popen.return_value = _make_mock_proc(pid=1000)

        with patch("bamboo_mcp_services.agents.supervisor_agent.agent.os.path.exists", return_value=True):
            cfg = SupervisorConfig(agents=[
                _daemon_cfg(name="dependent", depends_on_file="/some/cric.duckdb")
            ])
            sv = SupervisorAgent(config=cfg)
            sv.start()

        self.assertEqual(mock_popen.call_count, 1)

    @patch("bamboo_mcp_services.agents.supervisor_agent.agent.subprocess.Popen")
    @patch("bamboo_mcp_services.agents.supervisor_agent.agent.time.sleep")
    @patch("bamboo_mcp_services.agents.supervisor_agent.agent.time.monotonic")
    def test_dependency_file_appears_during_wait(self, mock_mono, mock_sleep, mock_popen):
        """Agent should start once the dependency file appears mid-wait."""
        mock_popen.return_value = _make_mock_proc(pid=1001)

        t0 = 1000.0
        mock_mono.side_effect = [t0, t0, t0, t0]

        dep_path = "/tmp/cric-test-sentinel.duckdb"
        # Call counter: first call to dep_path → False, subsequent → True.
        # All other paths (e.g. logging handler checking its own log file) are
        # passed through to the real os.path.exists so they never exhaust a list.
        import os as _real_os
        dep_call_count = [0]

        def _exists_side_effect(path):
            if path == dep_path:
                dep_call_count[0] += 1
                return dep_call_count[0] > 1
            return _real_os.path.exists(path)

        with patch(
            "bamboo_mcp_services.agents.supervisor_agent.agent.os.path.exists",
            side_effect=_exists_side_effect,
        ):
            cfg = SupervisorConfig(agents=[
                _daemon_cfg(
                    name="waiter",
                    depends_on_file=dep_path,
                    depends_timeout_s=10.0,
                )
            ])
            sv = SupervisorAgent(config=cfg)
            sv.start()

        self.assertEqual(mock_popen.call_count, 1)

    @patch("bamboo_mcp_services.agents.supervisor_agent.agent.subprocess.Popen")
    @patch("bamboo_mcp_services.agents.supervisor_agent.agent.time.sleep")
    @patch("bamboo_mcp_services.agents.supervisor_agent.agent.time.monotonic")
    def test_dependency_timeout_starts_agent_anyway(self, mock_mono, mock_sleep, mock_popen):
        """After depends_timeout_s, the agent starts even without the file."""
        mock_popen.return_value = _make_mock_proc(pid=1002)

        # monotonic() calls through _wait_for_dependency and into _launch_daemon:
        #   1. deadline = monotonic() + timeout                  → t0
        #   2. while monotonic() < deadline  (enters loop)       → t0        (t0 < t0+5 → True)
        #   3. while monotonic() < deadline  (exits loop)        → t0+6      (t0+6 < t0+5 → False)
        #   4. _launch_daemon: if monotonic() < next_restart_after → t0+6    (0.0 → no back-off)
        t0 = 1000.0
        timeout = 5.0
        mock_mono.side_effect = [t0, t0, t0 + timeout + 1, t0 + timeout + 1]

        with patch(
            "bamboo_mcp_services.agents.supervisor_agent.agent.os.path.exists",
            return_value=False,
        ):
            cfg = SupervisorConfig(agents=[
                _daemon_cfg(
                    name="impatient",
                    depends_on_file="/never/exists.db",
                    depends_timeout_s=timeout,
                )
            ])
            sv = SupervisorAgent(config=cfg)
            sv.start()

        # Agent must still be started despite missing dependency.
        self.assertEqual(mock_popen.call_count, 1)


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

    def test_status_flag_prints_json(self):
        import tempfile
        from bamboo_mcp_services.agents.supervisor_agent.cli import _print_status

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sup.yaml")
            with open(path, "w") as fh:
                fh.write("""
agents:
  - name: cric
    mode: daemon
    command: [bamboo-cric, --data, cric.duckdb]
""")
            with patch("builtins.print") as mock_print:
                rc = _print_status(path)

        self.assertEqual(rc, 0)
        mock_print.assert_called_once()
        printed = mock_print.call_args[0][0]
        parsed = json.loads(printed)
        self.assertIn("agents", parsed)
        self.assertEqual(parsed["agents"][0]["name"], "cric")


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

    def test_missing_command_is_retried(self):
        sv = SupervisorAgent(config=SupervisorConfig(agents=[
            AgentConfig(name="missing", mode=MODE_DAEMON, command=["/nonexistent/bamboo-cric"]),
        ]))
        sv.start()
        with self.assertLogs("bamboo_mcp_services.agents.supervisor_agent.agent", level="ERROR") as logs:
            sv.tick()
        self.assertTrue(any("Failed to launch daemon agent 'missing'" in m for m in logs.output))
        sv.stop()
