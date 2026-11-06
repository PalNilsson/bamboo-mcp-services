"""BigPanda job fetcher for periodic ingestion of per-queue job data.

This module handles downloading job JSON from the BigPanda monitoring service for a
configurable list of ATLAS computing queues and persisting the parsed data into the
DuckDB schema defined in :mod:`bamboo_mcp_services.common.storage.schema`.

Typical usage
-------------
The :class:`BigPandaJobsFetcher` is driven by the ingestion agent's ``_tick_impl``
method.  On each call to :meth:`BigPandaJobsFetcher.run_cycle` the fetcher checks
whether the configured interval has elapsed since the last full cycle.  If it has, it
iterates over all queues, downloading jobs data for each one and persisting to DuckDB.
A short sleep is inserted between consecutive queue requests to be polite to the server.

Connection lifetime
-------------------
Given a ``duckdb_path``, the fetcher opens the database once per queue — only
for that queue's write transaction — and closes it again with a
``CHECKPOINT`` (see
:func:`~bamboo_mcp_services.common.storage.duckdb_store.short_lived_connection`).
The HTTP download and the inter-queue sleep happen with the file closed, so
other processes can read ``jobs.duckdb`` for nearly all of a cycle, which
takes hours across the full ATLAS queue list.

Given a ``conn`` instead, it uses that connection and never closes it.  That
form exists for ``":memory:"`` databases and tests.

Retention
---------
With ``retention_days > 0``, every cycle starts by deleting rows whose
``_fetched_utc`` is older than that many days from ``jobs``,
``selectionsummary`` and ``errors_by_count``.  ``_fetched_utc`` is refreshed
each time BigPanda reports a job, so this removes jobs not seen for
``retention_days``.  DuckDB reuses the freed space but shrinks the file only
when the free space is at its end; see ``scripts/compact_duckdb.py``.
"""
from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, Optional

import duckdb
import requests

from bamboo_mcp_services.common.storage.duckdb_store import short_lived_connection
from bamboo_mcp_services.common.storage.schema import apply_schema

logger = logging.getLogger(__name__)

#: Base URL template for BigPanda jobs endpoint.
BIGPANDA_JOBS_URL = "https://bigpanda.cern.ch/jobs/?computingsite={queue}&json&hours=1"

#: Default list of queues to poll.
DEFAULT_QUEUES: list[str] = ["SWT2_CPB", "BNL"]

#: Default interval in seconds between full polling cycles (30 minutes).
DEFAULT_CYCLE_INTERVAL_S: int = 30 * 60

#: Default wait in seconds between successive queue downloads within one cycle.
DEFAULT_INTER_QUEUE_DELAY_S: int = 60

#: Tables purged by the retention step, all keyed on ``_fetched_utc``.
RETENTION_TABLES: tuple[str, ...] = ("jobs", "selectionsummary", "errors_by_count")

#: Timestamp format used for ``_fetched_utc`` values (naive UTC).
_TS_FORMAT = "%Y-%m-%d %H:%M:%S"


class BigPandaJobsFetcher:
    """Downloads and persists BigPanda job data for a list of queues.

    Each call to :meth:`run_cycle` checks whether the configured cycle interval has
    elapsed.  If so, it fetches jobs data for every queue in sequence, sleeping
    ``inter_queue_delay_s`` seconds between queues, and upserts the rows into the
    ``jobs``, ``selectionsummary``, and ``errors_by_count`` tables.

    Attributes:
        queues: List of BigPanda computing-site queue names to poll.
        cycle_interval_s: Minimum seconds between full cycles.
        inter_queue_delay_s: Seconds to sleep between consecutive queue fetches.
        retention_days: Rows older than this are purged at the start of each
            cycle; ``0`` disables the purge.
    """

    def __init__(
        self,
        conn: Optional[duckdb.DuckDBPyConnection],
        queues: list[str] | None = None,
        cycle_interval_s: int = DEFAULT_CYCLE_INTERVAL_S,
        inter_queue_delay_s: int = DEFAULT_INTER_QUEUE_DELAY_S,
        duckdb_path: Optional[str] = None,
        retention_days: int = 0,
    ) -> None:
        """Initialise the fetcher and make sure the schema exists.

        Exactly one of *conn* and *duckdb_path* must be given.

        Args:
            conn: An open, writable DuckDB connection that the fetcher uses for
                every write and does **not** close.  Pass ``None`` when giving
                *duckdb_path*.
            queues: Queue names to poll.  Defaults to :data:`DEFAULT_QUEUES`.
            cycle_interval_s: Seconds between full polling cycles.
            inter_queue_delay_s: Seconds to sleep between individual queue fetches
                within a single cycle.
            duckdb_path: Database file the fetcher opens for each queue write
                and closes again afterwards.
            retention_days: Purge rows whose ``_fetched_utc`` is older than this
                many days at the start of each cycle.  ``0`` keeps everything.

        Raises:
            ValueError: If both or neither of *conn* and *duckdb_path* are
                given, or if *retention_days* is negative.
        """
        if (conn is None) == (duckdb_path is None):
            raise ValueError("BigPandaJobsFetcher needs exactly one of conn and duckdb_path")
        if retention_days < 0:
            raise ValueError(f"retention_days must be >= 0, got {retention_days}")
        self._conn = conn
        self._duckdb_path = duckdb_path
        self.queues = queues if queues is not None else list(DEFAULT_QUEUES)
        self.cycle_interval_s = cycle_interval_s
        self.inter_queue_delay_s = inter_queue_delay_s
        self.retention_days = retention_days
        self._last_cycle_time: float = 0.0

        # Ensure tables exist.
        with self._writer() as c:
            apply_schema(c)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run_cycle(self, *, force: bool = False, one_shot: bool = False) -> bool:
        """Run a full polling cycle if the interval has elapsed.

        Args:
            force: If ``True``, ignore the interval check and always run.
            one_shot: If ``True``, skip the inter-queue delay.  Use this when
                running a single tick from the command line (``--once``) so the
                agent does not sit idle for ``inter_queue_delay_s`` seconds
                between queues before exiting.

        Returns:
            ``True`` if a cycle was executed, ``False`` if skipped because the
            interval has not yet elapsed.

        Raises:
            KeyboardInterrupt: Propagated immediately so the caller can shut down
                cleanly.  Other ``BaseException`` subclasses (e.g. ``SystemExit``)
                are likewise re-raised without being swallowed.
        """
        now = time.monotonic()
        if not force and (now - self._last_cycle_time) < self.cycle_interval_s:
            return False

        logger.info(
            "BigPandaJobsFetcher: starting cycle for queues %s", self.queues
        )
        fetched_utc = datetime.now(timezone.utc)

        if self.retention_days > 0:
            try:
                self.purge_expired(now_utc=fetched_utc)
            except Exception:
                # A failed purge must not cost a cycle of fresh data; it is
                # retried at the start of the next cycle.
                logger.exception("BigPandaJobsFetcher: retention purge failed")

        total = len(self.queues)
        for i, queue in enumerate(self.queues):
            logger.info(
                "BigPandaJobsFetcher: processing queue %r (%d/%d)",
                queue, i + 1, total,
            )
            try:
                self._fetch_and_persist(queue, fetched_utc)
            except KeyboardInterrupt:
                # User pressed Ctrl-C (or SIGINT arrived) — stop immediately.
                raise
            except RuntimeError as exc:
                # DuckDB intercepts KeyboardInterrupt during query execution and
                # re-raises it as RuntimeError("Query interrupted"), losing the
                # original exception type.  Detect this and propagate as
                # KeyboardInterrupt so the CLI shutdown path fires correctly.
                if "interrupted" in str(exc).lower() and isinstance(exc.__context__, KeyboardInterrupt):
                    raise KeyboardInterrupt() from exc
                logger.exception(
                    "BigPandaJobsFetcher: failed to fetch queue %s", queue
                )
            except BaseException as exc:
                if not isinstance(exc, Exception):
                    # SystemExit, GeneratorExit, etc. — always propagate.
                    raise
                logger.exception(
                    "BigPandaJobsFetcher: failed to fetch queue %s", queue
                )

            # Sleep between queues (skip after the last one, and skip entirely
            # in one-shot mode where the caller exits immediately after the cycle).
            if i < len(self.queues) - 1 and not one_shot:
                self._interruptible_sleep(self.inter_queue_delay_s)
            elif i < len(self.queues) - 1 and one_shot:
                logger.debug(
                    "BigPandaJobsFetcher: one-shot mode — skipping inter-queue delay"
                )

        self._last_cycle_time = time.monotonic()
        logger.info("BigPandaJobsFetcher: cycle complete")
        return True

    def _interruptible_sleep(self, seconds: int) -> None:
        """Sleep for *seconds* in short increments so Ctrl-C is responsive.

        A plain ``time.sleep(60)`` will block the process for the full duration
        before Python can react to a ``KeyboardInterrupt``.  Sleeping in 0.5 s
        slices gives sub-second Ctrl-C latency while still being gentle on the CPU.

        Args:
            seconds: Total sleep duration in seconds.

        Raises:
            KeyboardInterrupt: Re-raised immediately if received during sleep.
        """
        logger.debug(
            "BigPandaJobsFetcher: sleeping %ds before next queue", seconds
        )
        deadline = time.monotonic() + seconds
        slice_s = 0.5
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                time.sleep(min(slice_s, remaining))
            except KeyboardInterrupt:
                raise

    def purge_expired(self, now_utc: Optional[datetime] = None) -> dict[str, int]:
        """Delete rows older than ``retention_days`` from every retention table.

        All deletions run in one transaction.

        Args:
            now_utc: Reference time (timezone-aware UTC).  Defaults to now.

        Returns:
            ``{table: rows_deleted}`` for each table in :data:`RETENTION_TABLES`;
            an empty dict when retention is disabled.
        """
        if self.retention_days <= 0:
            return {}
        now = now_utc or datetime.now(timezone.utc)
        cutoff = (now - timedelta(days=self.retention_days)).strftime(_TS_FORMAT)
        deleted: dict[str, int] = {}
        with self._writer() as conn:
            conn.execute("BEGIN")
            try:
                for table in RETENTION_TABLES:
                    row = conn.execute(
                        f"DELETE FROM {table} WHERE _fetched_utc < CAST(? AS TIMESTAMP)", [cutoff]
                    ).fetchone()
                    deleted[table] = int(row[0]) if row else 0
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        logger.info(
            "BigPandaJobsFetcher: retention purge (older than %s UTC, %d days): %s",
            cutoff, self.retention_days,
            ", ".join(f"{t}={n}" for t, n in deleted.items()),
        )
        return deleted

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @contextmanager
    def _writer(self) -> Iterator[duckdb.DuckDBPyConnection]:
        """Yield a writable connection for one write.

        Yields:
            The injected connection (left open), or a short-lived connection to
            ``duckdb_path`` that is checkpointed and closed when the block ends.
        """
        if self._conn is not None:
            yield self._conn
            return
        assert self._duckdb_path is not None  # guaranteed by __init__
        with short_lived_connection(self._duckdb_path) as conn:
            yield conn

    def _fetch_and_persist(self, queue: str, fetched_utc: datetime) -> None:
        """Download jobs JSON for *queue* and upsert into DuckDB.

        All three table writes (``jobs``, ``selectionsummary``,
        ``errors_by_count``) are wrapped in a single transaction so that a
        concurrent reader always observes a consistent view of an entire
        fetch cycle for this queue — never a state where some tables have
        been updated and others have not.

        Args:
            queue: BigPanda computing-site identifier (e.g. ``"SWT2_CPB"``).
            fetched_utc: Timestamp to stamp every persisted row with.
        """
        url = BIGPANDA_JOBS_URL.format(queue=queue)
        logger.info("BigPandaJobsFetcher: fetching %s", url)

        response = requests.get(url, timeout=60)
        response.raise_for_status()
        payload: dict[str, Any] = response.json()

        jobs: list[dict[str, Any]] = payload.get("jobs", [])
        summary: list[dict[str, Any]] = payload.get("selectionsummary", [])
        errors: list[dict[str, Any]] = payload.get("errsByCount", [])

        logger.info(
            "BigPandaJobsFetcher: queue=%s jobs=%d summary=%d errors=%d",
            queue,
            len(jobs),
            len(summary),
            len(errors),
        )

        ts = fetched_utc.strftime(_TS_FORMAT)

        # The download above ran with the database closed; it is held only
        # for this transaction.
        with self._writer() as conn:
            conn.execute("BEGIN")
            try:
                self._upsert_jobs(conn, jobs, queue, ts)
                self._insert_summary(conn, summary, queue, ts)
                self._insert_errors(conn, errors, queue, ts)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    # ------------------------------------------------------------------
    # Per-table persistence helpers
    # ------------------------------------------------------------------

    #: Schema columns accepted by the ``jobs`` table.  API fields not in this
    #: set are silently ignored.  The two bookkeeping columns (_queue,
    #: _fetched_utc) are added by the ingestion agent and are not listed here.
    _JOBS_KNOWN_COLUMNS: frozenset[str] = frozenset({
        "pandaid", "jobdefinitionid", "schedulerid", "pilotid", "taskid",
        "jeditaskid", "reqid", "jobsetid", "workqueue_id",
        "creationtime", "modificationtime", "statechangetime",
        "proddbupdatetime", "starttime", "endtime",
        "creationhost", "modificationhost", "computingsite",
        "computingelement", "nucleus", "wn",
        "atlasrelease", "transformation", "homepackage", "cmtconfig",
        "container_name", "cpu_architecture_level",
        "prodserieslabel", "prodsourcelabel", "produserid", "gshare",
        "grid", "cloud", "homecloud", "transfertype", "resourcetype",
        "eventservice", "job_label", "category", "lockedby",
        "relocationflag", "jobname", "ipconnectivity", "processor_type",
        "assignedpriority", "currentpriority", "priorityrange",
        "jobsetrange", "attemptnr", "maxattempt", "failedattempt",
        "jobstatus", "jobsubstatus", "commandtopilot", "transexitcode",
        "piloterrorcode", "piloterrordiag", "exeerrorcode", "exeerrordiag",
        "superrorcode", "superrordiag", "ddmerrorcode", "ddmerrordiag",
        "brokerageerrorcode", "brokerageerrordiag",
        "jobdispatchererrorcode", "jobdispatchererrordiag",
        "taskbuffererrorcode", "taskbuffererrordiag",
        "errorinfo", "error_desc", "transformerrordiag",
        "proddblock", "dispatchdblock", "destinationdblock",
        "destinationse", "sourcesite", "destinationsite",
        "maxcpucount", "maxcpuunit", "maxdiskcount", "maxdiskunit",
        "minramcount", "minramunit", "corecount", "actualcorecount",
        "meancorecount", "maxwalltime",
        "cpuconsumptiontime", "cpuconsumptionunit", "cpuconversion",
        "cpuefficiency", "hs06", "hs06sec",
        "maxrss", "maxvmem", "maxswap", "maxpss",
        "avgrss", "avgvmem", "avgswap", "avgpss", "maxpssgbpercore",
        "totrchar", "totwchar", "totrbytes", "totwbytes",
        "raterchar", "ratewchar", "raterbytes", "ratewbytes",
        "diskio", "memoryleak", "memoryleakx2",
        "nevents", "ninputdatafiles", "inputfiletype", "inputfileproject",
        "inputfilebytes", "noutputdatafiles", "outputfilebytes",
        "outputfiletype",
        "durationsec", "durationmin", "duration",
        "waittimesec", "waittime", "pilotversion",
        "gco2_regional", "gco2_global",
        "jobmetrics", "jobinfo", "consumer",
    })

    def _upsert_jobs(
        self, conn: duckdb.DuckDBPyConnection, jobs: list[dict[str, Any]], queue: str, ts: str
    ) -> None:
        """Upsert job rows into the ``jobs`` table using a bulk DataFrame insert.

        DuckDB is a columnar engine optimised for bulk operations.  Using
        ``executemany`` with individual parameter sets is O(n) in the number of
        round-trips to the query engine and is extremely slow for large payloads
        (10k rows takes ~3 minutes).  Building a pandas DataFrame and executing a
        single ``INSERT OR REPLACE … SELECT * FROM df`` reduces that to well under
        one second for the same data.

        Args:
            conn: Writable connection inside the queue's transaction.
            jobs: List of job dicts from the BigPanda API.
            queue: Source queue name (stored in ``_queue``).
            ts: Fetch timestamp string in ``YYYY-MM-DD HH:MM:SS`` format.
        """
        import pandas as pd  # imported here to keep the module lightweight

        if not jobs:
            return

        # Determine which API-returned columns are in our schema.
        present_keys = sorted(
            k for k in jobs[0].keys() if k in self._JOBS_KNOWN_COLUMNS
        )
        # Fill in any keys that appear only in later rows.
        for job in jobs[1:]:
            for k in job:
                if k in self._JOBS_KNOWN_COLUMNS and k not in present_keys:
                    present_keys.append(k)
        present_keys.sort()

        if not present_keys:
            logger.warning(
                "BigPandaJobsFetcher: no known columns in jobs payload for queue %s",
                queue,
            )
            return

        col_list = present_keys + ["_queue", "_fetched_utc"]

        # Build the DataFrame in one pass — much faster than row-by-row appending.
        data: dict[str, Any] = {c: [job.get(c) for job in jobs] for c in present_keys}
        data["_queue"] = queue
        data["_fetched_utc"] = ts

        df = pd.DataFrame(data, columns=col_list)  # noqa: F841 — used by DuckDB via locals()
        cols_sql = ", ".join(col_list)

        # DuckDB resolves the name 'df' from the calling frame's local scope.
        conn.execute(
            f"INSERT OR REPLACE INTO jobs ({cols_sql}) SELECT {cols_sql} FROM df"
        )

        logger.debug(
            "BigPandaJobsFetcher: upserted %d job rows for queue %s", len(df), queue
        )

    def _insert_summary(
        self, conn: duckdb.DuckDBPyConnection, summary: list[dict[str, Any]], queue: str, ts: str
    ) -> None:
        """Insert selection summary rows, clearing previous rows for this queue first.

        Args:
            conn: Writable connection inside the queue's transaction.
            summary: List of summary dicts from the BigPanda API.
            queue: Source queue name.
            ts: Fetch timestamp string.
        """
        import pandas as pd

        conn.execute(
            "DELETE FROM selectionsummary WHERE _queue = ?", [queue]
        )
        if not summary:
            return

        data = {
            "id": list(range(len(summary))),
            "field": [item.get("field", "") for item in summary],
            "list_json": [json.dumps(item.get("list", []), default=str) for item in summary],
            "stats_json": [
                json.dumps(item["stats"], default=str) if "stats" in item else None
                for item in summary
            ],
            "_queue": queue,
            "_fetched_utc": ts,
        }
        df = pd.DataFrame(data)  # noqa: F841
        conn.execute(
            "INSERT INTO selectionsummary "
            "(id, field, list_json, stats_json, _queue, _fetched_utc) "
            "SELECT id, field, list_json, stats_json, _queue, _fetched_utc FROM df"
        )

    def _insert_errors(
        self, conn: duckdb.DuckDBPyConnection, errors: list[dict[str, Any]], queue: str, ts: str
    ) -> None:
        """Insert error-by-count rows, clearing previous rows for this queue first.

        Args:
            conn: Writable connection inside the queue's transaction.
            errors: List of error dicts from the BigPanda API.
            queue: Source queue name.
            ts: Fetch timestamp string.
        """
        import pandas as pd

        conn.execute(
            "DELETE FROM errors_by_count WHERE _queue = ?", [queue]
        )
        if not errors:
            return

        data = {
            "id": list(range(len(errors))),
            "error": [e.get("error") for e in errors],
            "codename": [e.get("codename") for e in errors],
            "codeval": [e.get("codeval") for e in errors],
            "diag": [e.get("diag") for e in errors],
            "error_desc_text": [e.get("desc") for e in errors],
            "example_pandaid": [e.get("example_pandaid") for e in errors],
            "count": [e.get("count") for e in errors],
            "pandalist_json": [json.dumps(e.get("pandalist"), default=str) for e in errors],
            "_queue": queue,
            "_fetched_utc": ts,
        }
        df = pd.DataFrame(data)  # noqa: F841
        conn.execute(
            "INSERT INTO errors_by_count "
            "(id, error, codename, codeval, diag, error_desc_text, example_pandaid, "
            "count, pandalist_json, _queue, _fetched_utc) "
            "SELECT id, error, codename, codeval, diag, error_desc_text, example_pandaid, "
            "count, pandalist_json, _queue, _fetched_utc FROM df"
        )
