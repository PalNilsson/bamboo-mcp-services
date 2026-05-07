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
    BACKOFF_RAPID_RESTART_COUNT,
    BACKOFF_RAPID_WINDOW_S,
)
from bamboo_mcp_services.agents.supervisor_agent.cli import (
    _load_config,
    build_parser,
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

    def test_no_backoff_on_first_restarts(self):
        # Up to BACKOFF_RAPID_RESTART_COUNT exits should produce zero back-off.
        for _ in range(BACKOFF_RAPID_RESTART_COUNT):
            self.state.record_exit(exit_code=1)
        self.assertEqual(self.state.compute_backoff_s(), 0.0)

    def test_backoff_escalates_beyond_threshold(self):
        for _ in range(BACKOFF_RAPID_RESTART_COUNT + 1):
            self.state.record_exit(exit_code=1)
        delay = self.state.compute_backoff_s()
        self.assertGreater(delay, 0.0)
        self.assertLessEqual(delay, 300.0)

    def test_backoff_old_exits_pruned(self):
        """Exits older than BACKOFF_RAPID_WINDOW_S should not contribute to back-off."""
        past = time.monotonic() - BACKOFF_RAPID_WINDOW_S - 1
        self.state.restart_times = [past] * (BACKOFF_RAPID_RESTART_COUNT + 5)
        # Trigger a prune by recording a new exit.
        self.state.record_exit(exit_code=0)
        # After pruning only the one new timestamp should remain.
        self.assertEqual(len(self.state.restart_times), 1)
        self.assertEqual(self.state.compute_backoff_s(), 0.0)

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
        live_proc = _make_mock_proc(pid=200, returncode=None)
        dead_proc = _make_mock_proc(pid=201, returncode=1)
        restarted_proc = _make_mock_proc(pid=202, returncode=None)

        mock_popen.side_effect = [live_proc, dead_proc, restarted_proc]

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
        # Inject enough rapid restarts to trigger back-off.
        now = time.monotonic()
        state.restart_times = [now] * (BACKOFF_RAPID_RESTART_COUNT + 2)

        # Simulate exit.
        sv._daemon_procs["crasher"].poll.return_value = 1
        sv.tick()

        # next_restart_after should be set in the future.
        self.assertGreater(state.next_restart_after, time.monotonic())

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

        # next_run defaults to 0.0, so first tick should dispatch it.
        sv.tick()

        mock_popen.assert_called_once()
        state = sv._scheduled_states["hourly"]
        self.assertEqual(state.run_count, 1)
        self.assertEqual(state.last_exit_code, 0)

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
        slow_proc.wait.side_effect = subprocess.TimeoutExpired(cmd=["slow"], timeout=1)
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
        sv.tick()

        slow_proc.kill.assert_called_once()
        state = sv._scheduled_states["slow"]
        self.assertEqual(state.last_exit_code, -1)

    @patch("bamboo_mcp_services.agents.supervisor_agent.agent.subprocess.Popen")
    def test_scheduled_interval_advanced_after_completion(self, mock_popen):
        proc = _make_mock_proc(pid=900, returncode=0)
        proc.wait.return_value = 0
        mock_popen.return_value = proc

        interval = 300.0
        cfg = SupervisorConfig(agents=[_scheduled_cfg(name="periodic", interval_s=interval)])
        sv = SupervisorAgent(config=cfg)
        sv.start()
        sv.tick()

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
        sv.tick()

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

        with patch("os.path.exists", return_value=True):
            cfg = SupervisorConfig(agents=[
                _daemon_cfg(name="dependent", depends_on_file="/some/cric.duckdb")
            ])
            sv = SupervisorAgent(config=cfg)
            sv.start()

        self.assertEqual(mock_popen.call_count, 1)

    @patch("bamboo_mcp_services.agents.supervisor_agent.agent.subprocess.Popen")
    @patch("bamboo_mcp_services.agents.supervisor_agent.agent.time.sleep")
    def test_dependency_file_appears_during_wait(self, mock_sleep, mock_popen):
        """Agent should start once the dependency file appears mid-wait."""
        mock_popen.return_value = _make_mock_proc(pid=1001)

        # exists() returns False once, then True.
        with patch("os.path.exists", side_effect=[False, True]):
            cfg = SupervisorConfig(agents=[
                _daemon_cfg(
                    name="waiter",
                    depends_on_file="/tmp/cric.duckdb",
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

        # Simulate monotonic advancing past the deadline quickly.
        start = 1000.0
        timeout = 5.0
        mock_mono.side_effect = [start, start, start + timeout + 1]

        with patch("os.path.exists", return_value=False):
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
