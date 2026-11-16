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

"""Tests for supervisor_agent.heartbeat (S-18).

The freshness predicate decides whether a liveness probe restarts the
container, so each of its boundaries has a test that fails if it moves.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pytest

from bamboo_mcp_services.agents.supervisor_agent.heartbeat import (
    HEARTBEAT_SCHEMA_VERSION,
    HeartbeatWriter,
    build_heartbeat,
    check_heartbeat,
)

NOW = 1_800_000_000.0


def _health(state: str = "running") -> dict:
    """Return a minimal ``HealthReport.to_dict()``.

    Args:
        state: Supervisor state.

    Returns:
        The health dictionary.
    """
    return {"name": "supervisor-agent", "state": state, "ok": True,
            "details": {"agents": {"cric": {"mode": "daemon", "pid": 77}}}}


def _write(path: Path, written_at: float, state: str = "running", **overrides) -> None:
    """Write a heartbeat document, optionally corrupting fields.

    Args:
        path: Destination.
        written_at: ``written_at_epoch`` value.
        state: Supervisor state recorded in the health section.
        **overrides: Top-level fields to replace.
    """
    doc = build_heartbeat(_health(state), pid=4242, now=written_at)
    doc.update(overrides)
    path.write_text(json.dumps(doc), encoding="utf-8")


@pytest.fixture
def beat(tmp_path: Path) -> Path:
    """Path for the heartbeat file.

    Args:
        tmp_path: pytest temporary directory.

    Returns:
        A path inside it.
    """
    return tmp_path / "heartbeat.json"


# ── check_heartbeat: the probe's decision ─────────────────────────────────────

def test_fresh_running_heartbeat_passes(beat: Path) -> None:
    _write(beat, NOW - 30)
    result = check_heartbeat(beat, max_age_s=120, now=NOW)
    assert result.ok, result.reason
    assert result.age_s == pytest.approx(30)
    assert result.heartbeat is not None and result.heartbeat["supervisor_pid"] == 4242


def test_age_equal_to_the_limit_passes(beat: Path) -> None:
    _write(beat, NOW - 120)
    assert check_heartbeat(beat, max_age_s=120, now=NOW).ok


def test_age_just_over_the_limit_fails(beat: Path) -> None:
    _write(beat, NOW - 120.5)
    result = check_heartbeat(beat, max_age_s=120, now=NOW)
    assert not result.ok
    assert "stale" in result.reason


def test_clock_stepped_back_does_not_fail(beat: Path) -> None:
    """A heartbeat 'from the future' is younger, never older: no spurious restart."""
    _write(beat, NOW + 300)
    assert check_heartbeat(beat, max_age_s=120, now=NOW).ok


@pytest.mark.parametrize("state", ["stopped", "failed", "starting", "stopping"])
def test_supervisor_not_running_fails_even_when_fresh(beat: Path, state: str) -> None:
    _write(beat, NOW, state=state)
    result = check_heartbeat(beat, max_age_s=120, now=NOW)
    assert not result.ok
    assert repr(state) in result.reason


def test_agent_trouble_does_not_fail_the_check(beat: Path) -> None:
    """Restarting agents is the supervisor's job, not the probe's."""
    doc = build_heartbeat(
        {"state": "running", "ok": False,
         "details": {"agents": {"cric": {"pid": None, "consecutive_fast_failures": 9}}}},
        pid=1, now=NOW,
    )
    beat.write_text(json.dumps(doc), encoding="utf-8")
    assert check_heartbeat(beat, max_age_s=120, now=NOW).ok


def test_missing_file_fails(beat: Path) -> None:
    result = check_heartbeat(beat, max_age_s=120, now=NOW)
    assert not result.ok
    assert "no heartbeat file" in result.reason
    assert result.age_s is None and result.heartbeat is None


@pytest.mark.parametrize("content, words", [
    ("{not json", "not JSON"),
    ("[1, 2]", "not a JSON object"),
])
def test_malformed_file_fails(beat: Path, content: str, words: str) -> None:
    beat.write_text(content, encoding="utf-8")
    result = check_heartbeat(beat, max_age_s=120, now=NOW)
    assert not result.ok
    assert words in result.reason


def test_wrong_schema_version_fails(beat: Path) -> None:
    _write(beat, NOW, schema_version=HEARTBEAT_SCHEMA_VERSION + 1)
    result = check_heartbeat(beat, max_age_s=120, now=NOW)
    assert not result.ok
    assert "schema_version" in result.reason


@pytest.mark.parametrize("value", [None, "1800000000", True])
def test_non_numeric_timestamp_fails(beat: Path, value) -> None:
    _write(beat, NOW, written_at_epoch=value)
    result = check_heartbeat(beat, max_age_s=120, now=NOW)
    assert not result.ok
    assert "written_at_epoch" in result.reason


def test_to_dict_is_json_serialisable(beat: Path) -> None:
    _write(beat, NOW - 12.34)
    out = json.loads(json.dumps(check_heartbeat(beat, 120, now=NOW).to_dict()))
    assert out["ok"] is True
    assert out["age_s"] == 12.3


# ── HeartbeatWriter ───────────────────────────────────────────────────────────

def test_writer_output_passes_the_check(beat: Path) -> None:
    writer = HeartbeatWriter(beat)
    assert writer.write(_health(), now=NOW)
    result = check_heartbeat(beat, max_age_s=120, now=NOW)
    assert result.ok, result.reason
    assert result.heartbeat is not None
    assert result.heartbeat["supervisor_pid"] == os.getpid()
    assert result.heartbeat["health"]["details"]["agents"]["cric"]["pid"] == 77


def test_writer_replaces_the_file_atomically(beat: Path) -> None:
    """No temporary file is left next to the heartbeat."""
    writer = HeartbeatWriter(beat)
    writer.write(_health(), now=NOW)
    writer.write(_health(), now=NOW + 30)
    assert [p.name for p in beat.parent.iterdir()] == [beat.name]
    assert json.loads(beat.read_text())["written_at_epoch"] == NOW + 30


def test_write_failure_is_logged_once_and_recovery_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    missing_dir = tmp_path / "not-yet"
    writer = HeartbeatWriter(missing_dir / "heartbeat.json")
    caplog.set_level(logging.DEBUG, logger="bamboo_mcp_services.agents.supervisor_agent.heartbeat")

    assert not writer.write(_health(), now=NOW)
    assert not writer.write(_health(), now=NOW + 30)
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1

    missing_dir.mkdir()
    assert writer.write(_health(), now=NOW + 60)
    assert any("writable again" in r.getMessage() for r in caplog.records if r.levelno == logging.INFO)
    assert writer.write(_health(), now=NOW + 90)
    assert sum("writable again" in r.getMessage() for r in caplog.records) == 1
