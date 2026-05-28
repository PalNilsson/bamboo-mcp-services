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

Typical usage
-------------
The :class:`CricQueuedataFetcher` is driven by the CRIC agent's
``_tick_impl`` method::

    fetcher = CricQueuedataFetcher(
        conn=store._conn,
        cric_path="/cvmfs/atlas.cern.ch/repo/sw/local/etc/cric_pandaqueues.json",
        refresh_interval_s=600,
    )
    # In each tick:
    fetcher.run_cycle()
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

import duckdb

from bamboo_mcp_services.common.panda.source import BaseSource
from bamboo_mcp_services.common.storage.duckdb_store import atomic_swap_table

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

    The fetcher does **not** own the DuckDB connection — lifecycle management
    (open / close) is the caller's responsibility.

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
        conn: duckdb.DuckDBPyConnection,
        cric_path: str,
        refresh_interval_s: int = 600,
    ) -> None:
        """Initialise the fetcher.

        Args:
            conn: An open, writable DuckDB connection.  The fetcher does
                **not** close this connection — lifecycle management is the
                caller's responsibility.
            cric_path: Filesystem path to the CRIC queuedata JSON file.
            refresh_interval_s: Minimum seconds between successive file reads.
                Defaults to 600 (10 minutes).
        """
        self._conn = conn
        self.cric_path = cric_path
        self.refresh_interval_s = refresh_interval_s

        self._last_attempt: float = 0.0       # monotonic time of last run_cycle attempt
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
        if now - self._last_attempt < self.refresh_interval_s:
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
        except Exception:
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

        # --- Build staging table (not yet visible under the live name) -----
        self._conn.execute("DROP TABLE IF EXISTS queuedata_staging")
        self._create_staging_table(schema)
        self._insert_rows(rows, table="queuedata_staging")

        # --- Atomic promotion: staging → queuedata -------------------------
        atomic_swap_table(self._conn, "queuedata_staging", "queuedata")

        return len(rows)

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

    def _create_staging_table(self, schema: dict[str, str]) -> None:
        """Create the ``queuedata_staging`` table with the inferred schema.

        Unlike the old ``_create_table`` helper this method never touches the
        live ``queuedata`` table — it only creates the staging target.

        Args:
            schema: ``{column_name: duckdb_type}`` mapping as returned by
                :func:`_infer_schema`.
        """
        cols_sql = ", ".join(f'"{col}" {dtype}' for col, dtype in schema.items())
        self._conn.execute(f"CREATE TABLE queuedata_staging ({cols_sql})")

    def _insert_rows(self, rows: list[dict[str, Any]], table: str = "queuedata") -> None:
        """Bulk-insert *rows* into *table*.

        Args:
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
        self._conn.executemany(
            f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})",
            tuples,
        )
