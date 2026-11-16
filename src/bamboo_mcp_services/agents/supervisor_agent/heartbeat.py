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

"""Supervisor heartbeat file (S-18).

The running supervisor rewrites one small JSON file after every tick: its
own health report, including every managed agent's state.  Anything that
wants to know whether the supervisor is alive *and making progress* reads the
file and checks its age — ``bamboo-supervisor --status`` does exactly that, and
a container liveness probe runs that command.

A file, not an HTTP endpoint: it needs no port, no thread, and no extra
dependency, it survives the supervisor for post-mortem reading, and a probe
that execs a command works the same under Docker and Kubernetes.

What the check decides
----------------------
The check is about the supervisor loop only.  It passes when the file was
written at most ``max_age_s`` ago by a supervisor in state ``running``.  The
state of the individual agents is reported but never fails the check: the
supervisor restarts dead agents itself, and a probe that restarted the whole
container because one agent was in back-off would turn one failure into all
of them.

Ages use the wall clock (``time.time``), since the writer and the reader are
different processes.  A clock stepped backwards makes the file look younger,
never older, so it cannot cause a spurious restart; a clock stepped forwards
by more than ``max_age_s`` can cause one.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Union

from bamboo_mcp_services.common.fs import atomic_write_text

logger = logging.getLogger(__name__)

#: Version of the heartbeat document layout.
HEARTBEAT_SCHEMA_VERSION = 1

#: Default maximum heartbeat age, in seconds, accepted by ``--status``.
#: Four ticks at the default 30 s poll interval.
DEFAULT_MAX_AGE_S = 120.0

#: Supervisor state in which the heartbeat is considered alive.
_RUNNING = "running"

PathLike = Union[str, "os.PathLike[str]"]


def build_heartbeat(health: Mapping[str, Any], pid: int, now: float) -> dict[str, Any]:
    """Build the heartbeat document.

    Args:
        health: The supervisor's ``HealthReport.to_dict()``.
        pid: The supervisor's process ID.
        now: Wall-clock time of writing, as ``time.time()``.

    Returns:
        A JSON-serialisable dictionary.
    """
    return {
        "schema_version": HEARTBEAT_SCHEMA_VERSION,
        "written_at_epoch": now,
        "written_at_utc": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
        "supervisor_pid": pid,
        "health": dict(health),
    }


class HeartbeatWriter:
    """Writes the heartbeat file atomically, and reports write failures once.

    A failure is logged at ERROR the first time and at DEBUG while it
    persists, so a full disk produces one line rather than one per tick; the
    recovery is logged at INFO.  The supervisor carries on either way: the
    file goes stale, and whatever reads it reports that.
    """

    def __init__(self, path: PathLike) -> None:
        """Initialise the writer.

        Args:
            path: Heartbeat file to write.
        """
        self._path = Path(path)
        self._failing = False

    @property
    def path(self) -> Path:
        """The heartbeat file path."""
        return self._path

    def write(self, health: Mapping[str, Any], now: Optional[float] = None) -> bool:
        """Write the heartbeat file.

        Args:
            health: The supervisor's ``HealthReport.to_dict()``.
            now: Wall-clock time; ``None`` means ``time.time()``.

        Returns:
            ``True`` if the file was written.
        """
        stamp = time.time() if now is None else now
        try:
            text = json.dumps(build_heartbeat(health, os.getpid(), stamp), indent=2, sort_keys=True)
            atomic_write_text(self._path, text + "\n")
        except (OSError, TypeError, ValueError) as exc:
            if self._failing:
                logger.debug("Heartbeat %s still not writable: %s", self._path, exc)
            else:
                logger.error(
                    "Cannot write heartbeat %s: %s — it will go stale and --status will fail.",
                    self._path, exc,
                )
            self._failing = True
            return False
        if self._failing:
            logger.info("Heartbeat %s writable again.", self._path)
        self._failing = False
        return True


@dataclass(frozen=True)
class HeartbeatCheck:
    """Result of :func:`check_heartbeat`.

    Reasons are plain ASCII: ``--status`` prints them as JSON, and a probe's
    output ends up in Kubernetes events.

    Attributes:
        ok: Whether the supervisor is alive and its loop is making progress.
        reason: One line saying why, for operators and probe events.
        age_s: Seconds since the heartbeat was written, or ``None`` when it
            could not be read.
        heartbeat: The parsed document, or ``None`` when it could not be read.
    """

    ok: bool
    reason: str
    age_s: Optional[float] = None
    heartbeat: Optional[Mapping[str, Any]] = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable form.

        Returns:
            ``ok``, ``reason``, ``age_s`` and ``heartbeat``.
        """
        return {
            "ok": self.ok,
            "reason": self.reason,
            "age_s": None if self.age_s is None else round(self.age_s, 1),
            "heartbeat": None if self.heartbeat is None else dict(self.heartbeat),
        }


def _read(path: Path) -> tuple[Optional[dict[str, Any]], str]:
    """Read and validate a heartbeat document.

    Args:
        path: Heartbeat file.

    Returns:
        ``(document, "")`` on success, ``(None, reason)`` otherwise.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, f"no heartbeat file at {path}: supervisor not running, or not started with this --heartbeat-file"
    except OSError as exc:
        return None, f"cannot read heartbeat {path}: {exc}"
    try:
        doc = json.loads(text)
    except ValueError as exc:
        return None, f"heartbeat {path} is not JSON: {exc}"
    if not isinstance(doc, dict):
        return None, f"heartbeat {path} is not a JSON object"
    if doc.get("schema_version") != HEARTBEAT_SCHEMA_VERSION:
        return None, (
            f"heartbeat {path} has schema_version {doc.get('schema_version')!r}, "
            f"expected {HEARTBEAT_SCHEMA_VERSION}"
        )
    stamp = doc.get("written_at_epoch")
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
        return None, f"heartbeat {path} has no numeric written_at_epoch"
    return doc, ""


def check_heartbeat(path: PathLike, max_age_s: float, now: Optional[float] = None) -> HeartbeatCheck:
    """Decide whether the supervisor that writes *path* is alive.

    Args:
        path: Heartbeat file.
        max_age_s: Oldest acceptable heartbeat, in seconds.  An age exactly
            equal to it passes.
        now: Wall-clock time; ``None`` means ``time.time()``.

    Returns:
        The verdict.  ``ok`` is ``False`` when the file is missing, unreadable,
        malformed, older than *max_age_s*, or written by a supervisor that was
        not running (a clean stop writes a final heartbeat in state
        ``stopped``).
    """
    doc, problem = _read(Path(path))
    if doc is None:
        return HeartbeatCheck(ok=False, reason=problem)
    current = time.time() if now is None else now
    age = current - float(doc["written_at_epoch"])
    health = doc.get("health")
    state = health.get("state") if isinstance(health, dict) else None
    if state != _RUNNING:
        return HeartbeatCheck(
            ok=False, age_s=age, heartbeat=doc,
            reason=f"supervisor state is {state!r} (last heartbeat {age:.0f}s ago)",
        )
    if age > max_age_s:
        return HeartbeatCheck(
            ok=False, age_s=age, heartbeat=doc,
            reason=f"heartbeat is stale: {age:.0f}s old, limit {max_age_s:.0f}s; the supervisor loop is stuck or dead",
        )
    return HeartbeatCheck(ok=True, age_s=age, heartbeat=doc, reason=f"supervisor running, heartbeat {age:.0f}s old")
