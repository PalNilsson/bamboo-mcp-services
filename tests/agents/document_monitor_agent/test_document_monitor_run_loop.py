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

"""document-monitor's run loop when a stop arrives mid-cycle (S-32).

Seen in the container smoke test on aipanda033: SIGTERM arrived while one
watch pair was being processed, the handler stopped all five agents, and the
``--once`` loop then called ``tick()`` on the next, stopped, agent.  That
raised ``RuntimeError`` and the orderly stop ended as a traceback and exit 1.

The agents here are stand-ins with the real :class:`AgentState`, so the loop's
own running check is what is exercised.
"""
from __future__ import annotations

from typing import Callable, Optional

import pytest

from bamboo_mcp_services.agents.base import AgentState
from bamboo_mcp_services.agents.document_monitor_agent import cli


class _FakeAgent:
    """Minimal agent: start/stop/tick and a real AgentState."""

    def __init__(self, name: str, on_tick: Optional[Callable[[], None]] = None) -> None:
        """Create the agent.

        Args:
            name: Agent name.
            on_tick: Called during ``tick()``, e.g. to simulate a signal.
        """
        self.name = name
        self.state = AgentState.NEW
        self.ticks = 0
        self._on_tick = on_tick

    def start(self) -> None:
        """Enter RUNNING."""
        self.state = AgentState.RUNNING

    def stop(self) -> None:
        """Enter STOPPED; idempotent."""
        self.state = AgentState.STOPPED

    def tick(self) -> None:
        """Count the tick, as the real agent refuses one when not running.

        Raises:
            RuntimeError: When called while not running, like ``Agent.tick``.
        """
        if self.state is not AgentState.RUNNING:
            raise RuntimeError(f"Agent '{self.name}' is not running (state={self.state.value}); cannot tick().")
        self.ticks += 1
        if self._on_tick is not None:
            self._on_tick()


def _agents_with_signal_during(index: int, count: int = 5) -> list[_FakeAgent]:
    """Build agents where the SIGTERM handler fires during agent *index*'s tick.

    Args:
        index: Which agent's tick receives the signal.
        count: Number of agents.

    Returns:
        The agents, in order.
    """
    agents: list[_FakeAgent] = []
    handler = cli._make_signal_handler(agents)  # type: ignore[arg-type]
    for i in range(count):
        on_tick = (lambda: handler(15, None)) if i == index else None
        agents.append(_FakeAgent(f"docs{i}", on_tick))
    return agents


def test_once_survives_a_stop_during_a_cycle() -> None:
    agents = _agents_with_signal_during(index=3)
    cli._run_agents(agents, once=True)  # type: ignore[arg-type]
    assert [a.ticks for a in agents] == [1, 1, 1, 1, 0]
    assert all(a.state is AgentState.STOPPED for a in agents)


def test_once_logs_the_skipped_agents(caplog: pytest.LogCaptureFixture) -> None:
    agents = _agents_with_signal_during(index=0, count=3)
    with caplog.at_level("INFO", logger=cli.logger.name):
        cli._run_agents(agents, once=True)  # type: ignore[arg-type]
    skipped = [r.getMessage() for r in caplog.records if "skipping its poll cycle" in r.getMessage()]
    assert skipped == [
        "Agent 'docs1' is not running; skipping its poll cycle.",
        "Agent 'docs2' is not running; skipping its poll cycle.",
    ]


def test_once_without_a_signal_ticks_every_agent_once() -> None:
    agents = [_FakeAgent(f"docs{i}") for i in range(3)]
    cli._run_agents(agents, once=True)  # type: ignore[arg-type]
    assert [a.ticks for a in agents] == [1, 1, 1]
    assert all(a.state is AgentState.STOPPED for a in agents)


def test_daemon_mode_stops_after_a_signal() -> None:
    """Daemon mode already had the check; pin it so both modes stay alike."""
    agents = _agents_with_signal_during(index=1, count=3)
    cli._run_agents(agents, once=False)  # type: ignore[arg-type]
    assert [a.ticks for a in agents] == [1, 1, 0]
    assert all(a.state is AgentState.STOPPED for a in agents)
