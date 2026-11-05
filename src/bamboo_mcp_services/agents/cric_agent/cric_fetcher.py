"""CRIC queuedata fetcher for periodic ingestion of PanDA queue metadata.

This module reads the CRIC ``cric_pandaqueues.json`` file (available via
CVMFS) and loads the full queuedata dictionary into a local DuckDB table.

On each call to :meth:`CricQueuedataFetcher.run_cycle` the fetcher checks
whether the configured refresh interval has elapsed.  If it has, it reads the
file, compares the content hash against the previous load, and — only if the
content has changed — performs a full ``DROP + CREATE + INSERT`` of the
``queuedata`` table.  This keeps the database small: no history is
accumulated, only the latest snapshot is retained.

The three internal ``_data``-suffix fields (``coreenergy_data``,
``corepower_data``, ``maxdiskio_data``) are silently dropped at ingestion
time and never written to the database.

Type inference
--------------
Column types are inferred dynamically from the data rather than from a fixed
DDL.  This is intentional: the CRIC schema evolves without notice and a
dynamic approach is more robust than a hardcoded table definition.  The
inference logic (``_to_cell_value``, ``_merge_type``, ``_infer_schema``) is
derived from the standalone ``json_to_duckdb.py`` utility script.

Connection lifetime
-------------------
Given a ``duckdb_path``, the fetcher opens the database only for the SQL part
of a load and closes it straight after, with a ``CHECKPOINT`` (see
:func:`~bamboo_mcp_services.common.storage.duckdb_store.short_lived_connection`).
Between loads it holds no lock, so other processes — Bamboo MCP's
``cric_query`` — can open the file.  Reading the CVMFS file, hashing it and
building the rows all happen before the database is opened.

Given a ``conn`` instead, it uses that connection and never closes it.  That
form exists for ``":memory:"`` databases and tests.

Typical usage
-------------
The :class:`CricQueuedataFetcher` is driven by the CRIC agent's
``_tick_impl`` method::

    fetcher = CricQueuedataFetcher(
        conn=None,
        cric_path="/cvmfs/atlas.cern.ch/repo/sw/local/etc/cric_pandaqueues.json",
        refresh_interval_s=600,
        duckdb_path="/data/bamboo/db/cric.db",
    )
    # In each tick:
    fetcher.run_cycle()
"""
from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

import duckdb

from bamboo_mcp_services.common.panda.source import BaseSource
from bamboo_mcp_services.common.storage.duckdb_store import (
    atomic_swap_table,
    is_lock_conflict,
    short_lived_connection,
)

logger = logging.getLogger(__name__)

#: Column name used to store the top-level JSON key (the queue name).
_ID_COLUMN = "queue"

#: Fields present in the CRIC JSON that carry no useful information and are
#: dropped before the data reaches DuckDB.
_SKIP_FIELDS: frozenset[str] = frozenset({
    "coreenergy_data",
    "corepower_data",
    "maxdiskio_data",
})

#: DuckDB type escalation order used during schema inference.
#: BIGINT < DOUBLE < TEXT — a column widens to TEXT only when it must.
_TYPE_ORDER: list[str] = ["BIGINT", "DOUBLE", "TEXT"]


# ---------------------------------------------------------------------------
# Type-inference helpers (derived from json_to_duckdb.py)
# ---------------------------------------------------------------------------

def _to_cell_value(v: Any) -> tuple[Any, str]:
    """Convert an arbitrary Python value to a DB-storable scalar.

    Returns:
        A ``(stored_value, inferred_duckdb_type)`` pair.  Non-scalar values
        (lists, dicts) are JSON-serialised to TEXT.
    """
    if v is None:
        return None, "TEXT"
    if isinstance(v, bool):
        # bool must be checked before int — bool is a subclass of int in Python.
        return v, "TEXT"
    if isinstance(v, int):
        return v, "BIGINT"
    if isinstance(v, float):
        return v, "DOUBLE"
    if isinstance(v, str):
        return v, "TEXT"
    # Lists, dicts, and anything else → compact JSON string.
    try:
        return json.dumps(v, ensure_ascii=False, separators=(",", ":")), "TEXT"
    except Exception:
        return str(v), "TEXT"


def _merge_type(t1: str, t2: str) -> str:
    """Return the wider of two DuckDB type strings."""
    if t1 == t2:
        return t1
    order = {t: i for i, t in enumerate(_TYPE_ORDER)}
    return _TYPE_ORDER[max(order.get(t1, 2), order.get(t2, 2))]


def _infer_schema(rows: list[dict[str, Any]]) -> dict[str, str]:
    """Infer DuckDB column types by scanning all rows.

    Mutates *rows* in place: each cell value is replaced by its coerced
    scalar form (the same value that will be inserted into DuckDB).

    Args:
        rows: Mutable list of row dicts to scan and coerce.

    Returns:
        A ``{column_name: duckdb_type}`` mapping.
    """
    col_types: dict[str, str] = {}
    for row in rows:
        for k, v in row.items():
            val, t = _to_cell_value(v)
            row[k] = val
            prev = col_types.get(k)
            col_types[k] = t if prev is None else _merge_type(prev, t)
    return col_types


# ---------------------------------------------------------------------------
# Fetcher
# ---------------------------------------------------------------------------

class CricQueuedataFetcher:
    """Reads ``cric_pandaqueues.json`` and loads it into the ``queuedata`` table.

    Each call to :meth:`run_cycle` is cheap: it is a no-op unless
    ``refresh_interval_s`` seconds have elapsed since the last attempt.  When
    the interval has elapsed, the file is read and its SHA-256 hash is compared
    to the hash from the previous successful load.  The database write is
    skipped when the content is unchanged, avoiding unnecessary churn.

    The fetcher either opens the database itself for each load
    (``duckdb_path``) or uses a connection it is given and never closes
    (``conn``).  See the module docstring.

    Attributes:
        cric_path: Filesystem path to ``cric_pandaqueues.json``.
        refresh_interval_s: Minimum seconds between file reads.
        last_refresh_utc: UTC timestamp of the most recent successful table
            load, or ``None`` if the table has never been loaded in this
            session.
        last_row_count: Number of queue records written during the most recent
            load, or ``None`` if no load has occurred yet.
    """

    def __init__(
        self,
        conn: Optional[duckdb.DuckDBPyConnection],
        cric_path: str,
        refresh_interval_s: int = 600,
        duckdb_path: Optional[str] = None,
    ) -> None:
        """Initialise the fetcher.

        Exactly one of *conn* and *duckdb_path* must be given.

        Args:
            conn: An open, writable DuckDB connection that the fetcher uses
                for every load and does **not** close.  Pass ``None`` when
                giving *duckdb_path*.
            cric_path: Filesystem path to the CRIC queuedata JSON file.
            refresh_interval_s: Minimum seconds between successive file reads.
                Defaults to 600 (10 minutes).
            duckdb_path: Database file the fetcher opens for each load and
                closes again afterwards, releasing the lock between loads.

        Raises:
            ValueError: If both or neither of *conn* and *duckdb_path* are
                given.
        """
        if (conn is None) == (duckdb_path is None):
            raise ValueError("CricQueuedataFetcher needs exactly one of conn and duckdb_path")
        self._conn = conn
        self._duckdb_path = duckdb_path
        self.cric_path = cric_path
        self.refresh_interval_s = refresh_interval_s

        # Monotonic time of the last attempt; None means "due now".  Not 0.0:
        # monotonic time counts from boot, so within refresh_interval_s of a
        # host reboot 0.0 would look like a recent attempt and skip the load.
        self._last_attempt: Optional[float] = None
        self._last_hash: str | None = None    # content hash of the last successful load

        # Exposed for health reporting
        self.last_refresh_utc: datetime | None = None
        self.last_row_count: int | None = None

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def run_cycle(self) -> bool:
        """Run one refresh cycle if the interval has elapsed.

        Reads the CRIC JSON file, checks whether the content has changed, and
        — if it has — replaces the ``queuedata`` table in its entirety.

        Returns:
            ``True`` if the table was refreshed this cycle, ``False`` if the
            cycle was skipped (interval not elapsed) or the file was unchanged.
        """
        now = time.monotonic()
        if self._last_attempt is not None and now - self._last_attempt < self.refresh_interval_s:
            return False

        # Record the attempt time before doing any I/O so that a failure does
        # not cause a tight retry loop.
        self._last_attempt = now

        try:
            snapshot = BaseSource().fetch_from_file(self.cric_path)
        except Exception:
            logger.exception(
                "cric_fetcher: failed to read '%s'", self.cric_path
            )
            return False

        if snapshot.content_hash == self._last_hash:
            logger.debug(
                "cric_fetcher: file unchanged (hash=%.12s), skipping load",
                snapshot.content_hash,
            )
            return False

        logger.info(
            "cric_fetcher: new content detected (hash=%.12s → %.12s), loading",
            self._last_hash or "none",
            snapshot.content_hash,
        )

        if not isinstance(snapshot.raw, dict):
            logger.error(
                "cric_fetcher: expected a top-level JSON object in '%s', "
                "got %s — skipping load",
                self.cric_path,
                type(snapshot.raw).__name__,
            )
            return False

        try:
            row_count = self._load(snapshot.raw)
        except Exception as exc:
            if is_lock_conflict(exc):
                # Another process held the file for longer than the connect
                # retries allow.  Retry on the next tick rather than waiting a
                # full refresh interval; the hash is not recorded, so the same
                # content is loaded then.
                self._last_attempt = None
                logger.error(
                    "cric_fetcher: %s is locked by another process — load deferred to the next tick (%s)",
                    self._duckdb_path, exc,
                )
            else:
                logger.exception("cric_fetcher: load failed")
            return False

        self._last_hash = snapshot.content_hash
        self.last_refresh_utc = datetime.now(timezone.utc)
        self.last_row_count = row_count
        logger.info(
            "cric_fetcher: loaded %d queue records into 'queuedata'", row_count
        )
        return True

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _load(self, data: dict[str, Any]) -> int:
        """Replace the ``queuedata`` table with the contents of *data*.

        This is a full replacement using a shadow-table rename so that
        concurrent readers always observe either the previous complete snapshot
        or the new one — never a window where the table is absent or only
        partially filled.

        The sequence is:

        1. Build all rows and infer the DuckDB schema (no I/O yet).
        2. DROP any leftover ``queuedata_staging`` from a prior crash.
        3. CREATE ``queuedata_staging`` and bulk-INSERT all rows into it.
        4. Call :func:`~bamboo_mcp_services.common.storage.duckdb_store.atomic_swap_table`
           which, in a single transaction, renames ``queuedata_staging`` →
           ``queuedata`` (demoting the old live table to ``queuedata_retiring``
           and immediately dropping it).

        Steps 1–3 happen outside any transaction; they operate on a table that
        no reader queries.  Only step 4 — a pure metadata rename — touches the
        live name, and it does so atomically.

        Step 1 runs before the database is opened, so with ``duckdb_path`` the
        file is locked only for steps 2–4 and the closing ``CHECKPOINT``.

        Args:
            data: Top-level ``{queue_name: {field: value, ...}}`` dict as
                parsed from ``cric_pandaqueues.json``.

        Returns:
            Number of rows inserted.
        """
        rows = self._build_rows(data)
        if not rows:
            logger.warning("cric_fetcher: no rows to insert — leaving existing table intact")
            return 0

        schema = _infer_schema(rows)   # mutates rows to coerced values in place

        # The id column is always TEXT regardless of what inference might say.
        schema[_ID_COLUMN] = "TEXT"

        with self._writer() as conn:
            # --- Build staging table (not yet visible under the live name) ---
            conn.execute("DROP TABLE IF EXISTS queuedata_staging")
            self._create_staging_table(conn, schema)
            self._insert_rows(conn, rows, table="queuedata_staging")

            # --- Atomic promotion: staging → queuedata -----------------------
            atomic_swap_table(conn, "queuedata_staging", "queuedata")

        return len(rows)

    @contextmanager
    def _writer(self) -> Iterator[duckdb.DuckDBPyConnection]:
        """Yield a writable connection for one load.

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

    def _build_rows(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        """Convert the top-level CRIC dict to a list of flat row dicts.

        The top-level key (queue name) becomes the value of the ``queue``
        column.  Skip fields listed in :data:`_SKIP_FIELDS` are silently
        dropped.  Payload values that are themselves dicts are included as
        normal columns; non-dict payloads are stored under the key ``data``.

        Args:
            data: Top-level CRIC queuedata dictionary.

        Returns:
            List of row dicts ready for schema inference and insertion.
        """
        rows: list[dict[str, Any]] = []
        for queue_name, payload in data.items():
            row: dict[str, Any] = {_ID_COLUMN: queue_name}
            if isinstance(payload, dict):
                for k, v in payload.items():
                    if k in _SKIP_FIELDS:
                        continue
                    row[k] = v
            else:
                row["data"] = payload
            rows.append(row)
        return rows

    def _create_staging_table(self, conn: duckdb.DuckDBPyConnection, schema: dict[str, str]) -> None:
        """Create the ``queuedata_staging`` table with the inferred schema.

        Unlike the old ``_create_table`` helper this method never touches the
        live ``queuedata`` table — it only creates the staging target.

        Args:
            conn: Writable connection for the current load.
            schema: ``{column_name: duckdb_type}`` mapping as returned by
                :func:`_infer_schema`.
        """
        cols_sql = ", ".join(f'"{col}" {dtype}' for col, dtype in schema.items())
        conn.execute(f"CREATE TABLE queuedata_staging ({cols_sql})")

    def _insert_rows(self, conn: duckdb.DuckDBPyConnection, rows: list[dict[str, Any]], table: str = "queuedata") -> None:
        """Bulk-insert *rows* into *table*.

        Args:
            conn: Writable connection for the current load.
            rows: Coerced row dicts (all values already converted to DB-safe
                scalars by :func:`_infer_schema`).
            table: Target table name.  Defaults to ``"queuedata"`` for
                backward compatibility, but callers should pass
                ``"queuedata_staging"`` during the shadow-swap sequence.
        """
        if not rows:
            return
        cols = list(rows[0].keys())
        col_list = ", ".join(f'"{c}"' for c in cols)
        placeholders = ", ".join("?" * len(cols))
        tuples = [tuple(r.get(c) for c in cols) for r in rows]
        conn.executemany(
            f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})",
            tuples,
        )
