"""Ingestion agent for fetching and normalizing PanDA data sources."""
from __future__ import annotations
import json
import uuid
import time
import logging
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional
from dataclasses import dataclass, field
from bamboo_mcp_services.agents.base import Agent
from bamboo_mcp_services.common.storage.duckdb_store import MEMORY_PATH, DuckDBStore
from bamboo_mcp_services.common.panda.source import BaseSource, RawSnapshot
from bamboo_mcp_services.agents.ingestion_agent.bigpanda_jobs_fetcher import (
    BigPandaJobsFetcher,
    DEFAULT_QUEUES,
    DEFAULT_CYCLE_INTERVAL_S,
    DEFAULT_INTER_QUEUE_DELAY_S,
)
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


@dataclass
class SourceConfig:
    """Configuration for a single data source.

    Attributes:
        name: Unique identifier for this source.
        type: Type of source (e.g., 'cric', 'bigpanda').
        mode: Fetch mode - 'file' for local files or 'url' for HTTP/HTTPS.
        path: File system path (used when mode='file').
        url: Remote URL (used when mode='url').
        interval_s: Minimum seconds between fetches for this source.
    """
    name: str
    type: str
    mode: str = 'file'  # or 'url'
    path: Optional[str] = None
    url: Optional[str] = None
    interval_s: int = 300


@dataclass
class BigPandaJobsConfig:
    """Configuration for the periodic BigPanda jobs downloader.

    Attributes:
        enabled: Whether to run the BigPanda jobs fetcher at all.
        queues: Fallback list of computing-site queue names.  Ignored when
            ``cric_path`` points to an existing file.
        cric_path: Optional path to a ``cric_pandaqueues.json`` file produced
            by the CRIC agent.  When the file exists, its top-level keys are
            used as the queue list and ``queues`` is ignored.
        max_queues: Maximum number of queues to process per cycle.  ``0`` (the
            default) means no limit — all discovered queues are polled.
        cycle_interval_s: Seconds between full polling cycles (default: 30 min).
        inter_queue_delay_s: Seconds to wait between consecutive queue fetches
            within a single cycle (default: 60 s).
        retention_days: Purge job rows not seen by BigPanda for this many days
            at the start of every cycle.  ``0`` (the default) keeps everything;
            the bundled YAML sets 7.
    """
    enabled: bool = True
    queues: List[str] = field(default_factory=lambda: list(DEFAULT_QUEUES))
    cric_path: Optional[str] = None
    max_queues: int = 0
    cycle_interval_s: int = DEFAULT_CYCLE_INTERVAL_S
    inter_queue_delay_s: int = DEFAULT_INTER_QUEUE_DELAY_S
    retention_days: int = 0


@dataclass
class IngestionAgentConfig:
    """Configuration for the IngestionAgent.

    Attributes:
        sources: List of generic data sources to ingest.
        duckdb_path: Path to DuckDB database file or ':memory:'.
        tick_interval_s: Seconds to sleep between tick() calls.
        bigpanda_jobs: Configuration for the periodic BigPanda jobs fetcher.
    """
    sources: List[SourceConfig]
    duckdb_path: str = ':memory:'
    tick_interval_s: float = 1.0
    bigpanda_jobs: BigPandaJobsConfig = field(default_factory=BigPandaJobsConfig)


class IngestionAgent(Agent):
    """Agent for periodic ingestion of PanDA data sources.

    The ingestion agent fetches data from configured sources (files or URLs),
    normalizes the data, and stores it in a DuckDB database. It tracks fetch
    intervals to avoid redundant fetches and records metadata for each snapshot.

    In addition to the generic source mechanism, the agent runs a dedicated
    :class:`~bamboo_mcp_services.agents.ingestion_agent.bigpanda_jobs_fetcher.BigPandaJobsFetcher`
    that periodically downloads per-queue job data from BigPanda and persists it
    in typed tables (``jobs``, ``selectionsummary``, ``errors_by_count``) using
    the schema from :mod:`bamboo_mcp_services.common.storage.schema`.

    For a database file the agent holds no connection between writes, so other
    processes can read the file while it runs: the fetcher opens it per queue
    write and each generic-source write opens its own short-lived store.  Only
    ``":memory:"`` keeps one store open for the agent's lifetime (exposed as
    :attr:`store`), because an in-memory database does not outlive its
    connection.
    """

    def __init__(self, name: str = 'ingestion-agent', config: Optional[IngestionAgentConfig] = None) -> None:
        """Initialize the ingestion agent.

        Args:
            name: Agent name (default: 'ingestion-agent').
            config: Optional IngestionAgentConfig. If not provided, uses
                default configuration with no sources and BigPanda jobs enabled.
        """
        super().__init__(name=name)
        self.config = config or IngestionAgentConfig(sources=[])
        self.store: Optional[DuckDBStore] = None
        self._source_last: Dict[str, float] = {}
        self._bpjobs_fetcher: Optional[BigPandaJobsFetcher] = None

    def _resolve_queues(self) -> List[str]:
        """Determine the list of queues to poll, applying cric_path and max_queues.

        If ``config.bigpanda_jobs.cric_path`` is set and the file exists, the
        queue names are extracted from the top-level keys of the JSON object.
        Otherwise, ``config.bigpanda_jobs.queues`` is used as a fallback.

        In both cases, if ``config.bigpanda_jobs.max_queues`` is greater than
        zero the list is truncated to that length.

        Returns:
            Resolved (and possibly truncated) list of queue names.
        """
        bpcfg = self.config.bigpanda_jobs
        queues: List[str]

        cric_path = bpcfg.cric_path
        if cric_path:
            import os
            if os.path.exists(cric_path):
                try:
                    with open(cric_path, 'r') as fh:
                        data = json.load(fh)
                    queues = list(data.keys())
                    logger.info(
                        "IngestionAgent: loaded %d queues from CRIC file %s",
                        len(queues), cric_path,
                    )
                except Exception as exc:
                    logger.warning(
                        "IngestionAgent: failed to read CRIC file %s (%s) — falling back to queues list",
                        cric_path, exc,
                    )
                    queues = list(bpcfg.queues)
            else:
                logger.warning(
                    "IngestionAgent: cric_path %r does not exist — falling back to queues list",
                    cric_path,
                )
                queues = list(bpcfg.queues)
        else:
            queues = list(bpcfg.queues)

        if bpcfg.max_queues and bpcfg.max_queues > 0:
            original_count = len(queues)
            queues = queues[:bpcfg.max_queues]
            if len(queues) < original_count:
                logger.info(
                    "IngestionAgent: max_queues=%d — using %d of %d available queues",
                    bpcfg.max_queues, len(queues), original_count,
                )

        return queues

    def _start_impl(self) -> None:
        """Initialise the BigPanda jobs fetcher and, for ``":memory:"``, the store.

        For a database file the store is opened and closed once here, so a bad
        path or a lock held by another process fails the start loudly, and the
        fetcher applies the schema with its own short-lived connection.

        Raises:
            duckdb.Error: If the database file cannot be opened.
        """
        path = self.config.duckdb_path
        if path == MEMORY_PATH:
            self.store = DuckDBStore(path)
        else:
            DuckDBStore(path).close()

        bpcfg = self.config.bigpanda_jobs
        if bpcfg.enabled:
            queues = self._resolve_queues()
            logger.info(
                "IngestionAgent: BigPanda jobs fetcher enabled – queues=%s "
                "cycle_interval=%ds inter_queue_delay=%ds retention_days=%s",
                queues,
                bpcfg.cycle_interval_s,
                bpcfg.inter_queue_delay_s,
                bpcfg.retention_days or "off",
            )
            self._bpjobs_fetcher = BigPandaJobsFetcher(
                conn=self.store._conn if self.store is not None else None,
                queues=queues,
                cycle_interval_s=bpcfg.cycle_interval_s,
                inter_queue_delay_s=bpcfg.inter_queue_delay_s,
                duckdb_path=None if self.store is not None else path,
                retention_days=bpcfg.retention_days,
            )
        else:
            logger.info("IngestionAgent: BigPanda jobs fetcher disabled")

    @contextmanager
    def _store_session(self) -> Iterator[DuckDBStore]:
        """Yield a store for one generic-source write.

        Yields:
            The lifetime in-memory store, or a short-lived store on the
            database file that is checkpointed and closed when the block ends.
        """
        if self.store is not None:
            yield self.store
            return
        with DuckDBStore(self.config.duckdb_path) as store:
            yield store

    def tick_once(self) -> None:
        """Run a single one-shot tick with the inter-queue delay suppressed.

        Equivalent to :meth:`tick` but passes ``one_shot=True`` to the BigPanda
        jobs fetcher so the agent exits promptly after downloading all queues,
        without sitting idle for ``inter_queue_delay_s`` seconds between them.

        Intended for use with the ``--once`` CLI flag.

        Raises:
            RuntimeError: If the agent is not in RUNNING state.
        """
        if self._state.value != 'running':
            raise RuntimeError(
                f"Agent '{self._name}' is not running (state={self._state.value}); cannot tick_once()."
            )
        import datetime as _dt
        self._last_tick_utc = _dt.datetime.now(_dt.timezone.utc)
        try:
            self._tick_impl(one_shot=True)
            self._last_success_utc = _dt.datetime.now(_dt.timezone.utc)
            self._last_error = None
        except Exception as exc:
            self._mark_failed(exc)
            raise

    def _tick_impl(self, *, one_shot: bool = False) -> None:
        """Fetch and ingest data from all configured sources, then run BigPanda jobs cycle.

        For each generic source, checks if the interval has elapsed since the last
        fetch. If so, fetches the data, records a snapshot, normalizes the
        data, and stores it in a history table.

        After processing generic sources, triggers the BigPanda jobs fetcher which
        will download job data for all configured queues if its own interval has elapsed.

        Args:
            one_shot: Passed through to :meth:`BigPandaJobsFetcher.run_cycle`.
                When ``True``, the inter-queue delay is skipped so the agent
                exits promptly after a single ``--once`` invocation.
        """
        now = time.time()
        for s in self.config.sources:
            last = self._source_last.get(s.name, 0)
            if now - last < s.interval_s:
                continue
            try:
                raw = self._fetch_source(s)
                rows = self._normalize(s, raw)
                with self._store_session() as store:
                    store.record_snapshot(str(uuid.uuid4()), s.name, True, raw.content_hash, None)
                    store.write_table(f"{s.name}_history", rows)
                self._source_last[s.name] = now
            except Exception as exc:
                logger.warning("IngestionAgent: source %r failed: %s", s.name, exc)
                try:
                    with self._store_session() as store:
                        store.record_snapshot(str(uuid.uuid4()), s.name, False, None, str(exc))
                except Exception:
                    logger.exception("IngestionAgent: could not record the failed snapshot for %r", s.name)

        # Run the BigPanda jobs fetcher cycle (no-op if interval hasn't elapsed).
        if self._bpjobs_fetcher is not None:
            try:
                self._bpjobs_fetcher.run_cycle(one_shot=one_shot)
            except KeyboardInterrupt:
                raise
            except Exception:
                logger.exception("IngestionAgent: BigPanda jobs fetcher cycle failed")

    def _stop_impl(self) -> None:
        """Release the fetcher and close the in-memory store, if any."""
        self._bpjobs_fetcher = None
        if self.store is not None:
            self.store.close()
            self.store = None

    def _fetch_source(self, s: SourceConfig) -> RawSnapshot:
        """Fetch data from a single source.

        Args:
            s: Source configuration.

        Returns:
            RawSnapshot containing the fetched data.

        Raises:
            RuntimeError: If the source configuration is invalid.
        """
        src = BaseSource()
        if s.mode == 'file' and s.path:
            return src.fetch_from_file(s.path)
        if s.mode == 'url' and s.url:
            return src.fetch_from_url(s.url)
        raise RuntimeError('invalid source config')

    def _normalize(self, s: SourceConfig, raw: RawSnapshot) -> List[Dict[str, Any]]:
        """Normalize raw snapshot data into structured rows.

        Args:
            s: Source configuration.
            raw: Raw snapshot to normalize.

        Returns:
            List of normalized data dictionaries.
        """
        return [{'payload': raw.raw, 'fetched_utc': datetime.now(timezone.utc).isoformat()}]
