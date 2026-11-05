"""CRIC agent for periodic ingestion of PanDA queue metadata.

This module provides :class:`CricAgent`, a lightweight agent that periodically
reads the CRIC ``cric_pandaqueues.json`` file from CVMFS and stores the latest
snapshot in a local DuckDB database.

The agent follows the standard lifecycle defined in
:class:`~bamboo_mcp_services.agents.base.Agent`:

- :meth:`~CricAgent.start` opens the DuckDB store and initialises the fetcher.
- :meth:`~CricAgent.tick` delegates to the fetcher, which is a no-op unless
  ``refresh_interval_s`` seconds have elapsed and the file content has changed.
- :meth:`~CricAgent.stop` closes the store.

The ``queuedata`` table is replaced in its entirety on each successful refresh;
no history is accumulated.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timezone
from typing import Any, Mapping, Optional

from bamboo_mcp_services.agents.base import Agent
from bamboo_mcp_services.common.storage.duckdb_store import (
    MEMORY_PATH,
    DuckDBStore,
    short_lived_connection,
)
from bamboo_mcp_services.agents.cric_agent.cric_fetcher import CricQueuedataFetcher

logger = logging.getLogger(__name__)


@dataclass
class CricAgentConfig:
    """Configuration for :class:`CricAgent`.

    Attributes:
        cric_path: Filesystem path to ``cric_pandaqueues.json``.  On
            production machines this is the CVMFS path
            ``/cvmfs/atlas.cern.ch/repo/sw/local/etc/cric_pandaqueues.json``.
        duckdb_path: Path to the DuckDB database file.  Must be set
            explicitly — there is no default.  Supplied via ``--data PATH``
            on the CLI.
        refresh_interval_s: Minimum seconds between successive file reads.
            Defaults to 600 (10 minutes).
        tick_interval_s: Seconds to sleep between tick() calls in the run
            loop.  Defaults to 60.0.
    """

    cric_path: str
    duckdb_path: str
    refresh_interval_s: int = 600
    tick_interval_s: float = 60.0


class CricAgent(Agent):
    """Agent that keeps a local DuckDB snapshot of CRIC queuedata.

    On each tick the agent delegates to
    :class:`~bamboo_mcp_services.agents.cric_agent.cric_fetcher.CricQueuedataFetcher`,
    which re-reads ``cric_pandaqueues.json`` only when the configured
    refresh interval has elapsed.  Within that check the fetcher compares
    the file's SHA-256 hash to the previous load and skips the database
    write when the content is unchanged.

    The ``queuedata`` table in ``duckdb_path`` always reflects the most
    recent snapshot; no historical rows are retained.

    For a database file the agent holds no connection between loads: the
    fetcher opens the file for each load and closes it afterwards, so other
    processes can read ``cric.db`` while the agent runs.  Only ``":memory:"``
    keeps one connection open for the agent's lifetime, because an in-memory
    database does not outlive its connection.
    """

    def __init__(
        self,
        name: str = "cric-agent",
        config: Optional[CricAgentConfig] = None,
    ) -> None:
        """Initialise the CRIC agent.

        Args:
            name: Agent name (default: ``'cric-agent'``).
            config: Agent configuration.  Must be supplied; the only reason
                the parameter is typed ``Optional`` is to satisfy the base
                class signature pattern used across all agents.

        Raises:
            ValueError: If *config* is ``None``.
        """
        super().__init__(name=name)
        if config is None:
            raise ValueError("CricAgentConfig must be provided")
        self.config = config
        self._store: Optional[DuckDBStore] = None
        self._fetcher: Optional[CricQueuedataFetcher] = None

    # ------------------------------------------------------------------
    # Agent lifecycle hooks
    # ------------------------------------------------------------------

    def _start_impl(self) -> None:
        """Initialise the fetcher, checking that the database can be opened.

        For a database file this opens and closes the file once, so that a bad
        path or a lock held by another process fails the start loudly instead
        of surfacing at the first load.

        Raises:
            duckdb.Error: If the database file cannot be opened.
        """
        path = self.config.duckdb_path
        if path == MEMORY_PATH:
            self._store = DuckDBStore(path)
            self._fetcher = CricQueuedataFetcher(
                conn=self._store._conn,
                cric_path=self.config.cric_path,
                refresh_interval_s=self.config.refresh_interval_s,
            )
            connection_mode = "held open (in-memory)"
        else:
            with short_lived_connection(path):
                pass
            self._fetcher = CricQueuedataFetcher(
                conn=None,
                cric_path=self.config.cric_path,
                refresh_interval_s=self.config.refresh_interval_s,
                duckdb_path=path,
            )
            connection_mode = "opened per load"
        logger.info(
            "CricAgent started: cric_path=%s  duckdb=%s (%s)  refresh_interval=%ds",
            self.config.cric_path,
            path,
            connection_mode,
            self.config.refresh_interval_s,
        )

    def _tick_impl(self) -> None:
        """Run one fetcher cycle (no-op if the interval has not elapsed)."""
        assert self._fetcher is not None  # guaranteed by _start_impl
        self._fetcher.run_cycle()

    def _stop_impl(self) -> None:
        """Release the fetcher and close the in-memory store, if any."""
        self._fetcher = None
        if self._store is not None:
            self._store.close()
            self._store = None
        logger.info("CricAgent stopped")

    # ------------------------------------------------------------------
    # Health reporting
    # ------------------------------------------------------------------

    def _health_details(self) -> Mapping[str, Any]:
        """Return CRIC-specific health metrics for the health report.

        Returns:
            Dictionary with keys:

            * ``cric_path`` — path being watched.
            * ``duckdb_path`` — database file path.
            * ``refresh_interval_s`` — configured interval in seconds.
            * ``last_refresh_utc`` — ISO 8601 timestamp of the last
              successful table load, or ``None``.
            * ``last_hash`` — first 12 characters of the content hash
              from the last load, or ``None``.
            * ``last_row_count`` — number of queue records written during
              the last load, or ``None``.
        """
        last_refresh = None
        last_hash = None
        last_row_count = None

        if self._fetcher is not None:
            if self._fetcher.last_refresh_utc is not None:
                last_refresh = (
                    self._fetcher.last_refresh_utc
                    .astimezone(timezone.utc)
                    .isoformat()
                )
            if self._fetcher._last_hash is not None:
                last_hash = self._fetcher._last_hash[:12]
            last_row_count = self._fetcher.last_row_count

        return {
            "cric_path": self.config.cric_path,
            "duckdb_path": self.config.duckdb_path,
            "refresh_interval_s": self.config.refresh_interval_s,
            "last_refresh_utc": last_refresh,
            "last_hash": last_hash,
            "last_row_count": last_row_count,
        }
