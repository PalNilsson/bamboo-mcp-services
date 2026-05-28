"""DuckDB storage backend for agent data persistence."""
from __future__ import annotations
import duckdb
from datetime import datetime, timezone
from typing import Optional, Any
import json


def atomic_swap_table(
    conn: duckdb.DuckDBPyConnection,
    staging_name: str,
    live_name: str,
) -> None:
    """Atomically promote *staging_name* to become the new *live_name*.

    The swap is performed inside a single transaction using ``ALTER TABLE
    RENAME``, which is a metadata-only operation in DuckDB (no data movement).
    Readers that hold an open read snapshot before the transaction commits will
    continue to see the old table; any reader that opens a new transaction after
    the commit will see the new one.  There is no window where the table is
    absent or partially filled.

    The pattern is::

        BEGIN
          [if live exists]  ALTER TABLE <live>    RENAME TO <live>_retiring
                            ALTER TABLE <staging> RENAME TO <live>
          [if live exists]  DROP TABLE <live>_retiring
        COMMIT

    On the very first run *live_name* does not yet exist, so the rename of the
    old live table is skipped.  The staging table is simply renamed to *live*.

    A stale ``<live>_retiring`` table (left over from a previous crash between
    the rename and the drop) is cleaned up at the start of every call so it
    does not block the rename.

    Args:
        conn: An open, writable DuckDB connection.
        staging_name: Name of the fully-populated staging table to promote.
        live_name: Logical name that callers use to query the data.

    Raises:
        Exception: Re-raises any DuckDB error after issuing ``ROLLBACK``.
    """
    retiring_name = f"{live_name}_retiring"

    # Clean up any stale retiring table from a previous crash.
    conn.execute(f"DROP TABLE IF EXISTS {retiring_name}")

    live_exists = bool(
        conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = ?",
            [live_name],
        ).fetchone()
    )

    conn.execute("BEGIN")
    try:
        if live_exists:
            conn.execute(f"ALTER TABLE {live_name} RENAME TO {retiring_name}")
        conn.execute(f"ALTER TABLE {staging_name} RENAME TO {live_name}")
        if live_exists:
            conn.execute(f"DROP TABLE {retiring_name}")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


class DuckDBStore:
    """DuckDB-based storage for agent data and snapshots.

    Provides methods for storing data snapshots, recording metadata,
    and managing structured data tables.

    Attributes:
        path: Database file path or \":memory:\" for in-memory database.
    """

    def __init__(self, path: str = ":memory:") -> None:
        """Initialize the DuckDB store.

        Args:
            path: Path to the DuckDB database file. Use \":memory:\" for
                an in-memory database (default).
        """
        self.path = path
        self._conn = duckdb.connect(database=path, read_only=False)
        self._init_meta()

    def _init_meta(self) -> None:
        """Initialize metadata tables and extensions.

        Creates the snapshots table if it doesn't exist and attempts
        to install the sqlite_scannable extension.
        """
        self._conn.execute("""
        CREATE TABLE IF NOT EXISTS snapshots (
            snapshot_id TEXT PRIMARY KEY,
            source TEXT,
            fetched_utc TIMESTAMP,
            content_hash TEXT,
            ok BOOLEAN,
            error TEXT
        );
        """)
        try:
            self._conn.execute("INSTALL sqlite_scannable")
        except Exception:
            pass

    def write_table(self, table_name: str, rows: list[dict[str, Any]], overwrite: bool = False) -> None:
        """Write data rows to a table.

        When *overwrite* is ``True`` the new rows are first written into a
        temporary staging table (``<table_name>_staging``), then the staging
        table is atomically promoted to become the live table via
        :func:`atomic_swap_table`.  This ensures that concurrent readers never
        observe a window where the table is absent or partially filled.

        When *overwrite* is ``False`` the table is created if absent and rows
        are appended directly.

        Args:
            table_name: Name of the target table.
            rows: List of dictionaries to insert. Each row is stored as JSON.
            overwrite: If True, replace the table contents atomically using a
                staging swap.  If False, create the table only if it doesn't
                exist and append rows.
        """
        if not rows:
            return
        if overwrite:
            staging = f"{table_name}_staging"
            # Clean up any leftover staging table from a previous crash.
            self._conn.execute(f"DROP TABLE IF EXISTS {staging}")
            self._conn.execute(
                f"CREATE TABLE {staging} (data JSON, updated_utc TIMESTAMP)"
            )
            for r in rows:
                self._conn.execute(
                    f"INSERT INTO {staging} VALUES (?, ?)",
                    [json.dumps(r, default=str), datetime.now(timezone.utc)],
                )
            atomic_swap_table(self._conn, staging, table_name)
        else:
            self._conn.execute(
                f"CREATE TABLE IF NOT EXISTS {table_name} (data JSON, updated_utc TIMESTAMP)"
            )
            for r in rows:
                self._conn.execute(
                    "INSERT INTO {tn} VALUES (?, ?)".format(tn=table_name),
                    [json.dumps(r, default=str), datetime.now(timezone.utc)],
                )

    def record_snapshot(self, snapshot_id: str, source: str, ok: bool, content_hash: Optional[str] = None, error: Optional[str] = None) -> None:
        """Record metadata for a data snapshot.

        Args:
            snapshot_id: Unique identifier for this snapshot.
            source: Origin identifier (e.g., file path or URL).
            ok: Whether the snapshot was fetched successfully.
            content_hash: SHA-256 hash of the content, if available.
            error: Error message if the fetch failed.
        """
        self._conn.execute(
            "INSERT OR REPLACE INTO snapshots VALUES (?, ?, ?, ?, ?, ?)",
            [snapshot_id, source, datetime.now(timezone.utc), content_hash, ok, error],
        )
