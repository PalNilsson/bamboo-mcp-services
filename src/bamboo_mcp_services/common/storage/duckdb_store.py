"""DuckDB storage backend for agent data persistence.

Connection lifetime
-------------------
DuckDB allows *either* one process that reads and writes a database file *or*
any number of processes that only read it — never both at once.  A writer that
keeps its connection open therefore locks every other process out of the file
for as long as it runs, and a reader that keeps its connection open blocks the
writer in the same way.

Agents that write a database another process reads must therefore hold the
file only while they write.  :func:`short_lived_connection` is the one way to
do that here: it opens the file (retrying briefly if another process holds the
lock), yields the connection, runs ``CHECKPOINT`` so the data reaches the main
database file rather than waiting in the write-ahead log, and always closes.
"""
from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from types import TracebackType
from typing import Any, Iterator, Optional, Type

import duckdb

logger = logging.getLogger(__name__)

#: Path that selects an in-memory database.  It cannot be shared between
#: processes, so code that would otherwise use short-lived connections keeps a
#: single connection open for it instead.
MEMORY_PATH = ":memory:"

#: How many times :func:`connect_with_retry` tries to open a locked file.
LOCK_RETRY_ATTEMPTS = 10

#: Seconds between attempts in :func:`connect_with_retry`.
LOCK_RETRY_DELAY_S = 0.5

#: Lower-cased text of DuckDB's error for a file locked by another process.
_LOCK_CONFLICT_TEXT = "could not set lock on file"


def is_lock_conflict(exc: BaseException) -> bool:
    """Return whether *exc* is DuckDB refusing a file lock held by another process.

    Args:
        exc: Any exception.

    Matches DuckDB's message text rather than the word "lock" alone, because
    the message also contains the file path, and a path such as
    ``/data/blocks/x.db`` in an unrelated error must not be retried.

    Returns:
        ``True`` for the ``IOException`` DuckDB raises with "Could not set lock
        on file"; ``False`` for everything else, including other I/O errors
        such as a missing directory, which must not be retried.
    """
    return isinstance(exc, duckdb.IOException) and _LOCK_CONFLICT_TEXT in str(exc).lower()


def connect_with_retry(
    path: str,
    *,
    read_only: bool,
    attempts: Optional[int] = None,
    delay_s: Optional[float] = None,
) -> duckdb.DuckDBPyConnection:
    """Open *path*, retrying while another process holds its lock.

    Only lock conflicts are retried; any other error is raised immediately.

    Args:
        path: Database file path.
        read_only: Open read-only (shared) instead of read-write (exclusive).
        attempts: Total attempts.  ``None`` reads :data:`LOCK_RETRY_ATTEMPTS`
            at call time, so tests can shorten it.
        delay_s: Seconds between attempts.  ``None`` reads
            :data:`LOCK_RETRY_DELAY_S` at call time.

    Returns:
        An open connection the caller must close.

    Raises:
        duckdb.IOException: If the lock is still held after the last attempt,
            or on any I/O error that is not a lock conflict.
    """
    n = LOCK_RETRY_ATTEMPTS if attempts is None else attempts
    delay = LOCK_RETRY_DELAY_S if delay_s is None else delay_s
    for attempt in range(1, max(n, 1) + 1):
        try:
            return duckdb.connect(database=path, read_only=read_only)
        except duckdb.IOException as exc:
            if not is_lock_conflict(exc) or attempt >= n:
                raise
            logger.debug(
                "DuckDB file %s is locked by another process (attempt %d/%d) — retrying in %.1fs",
                path, attempt, n, delay,
            )
            time.sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover — loop always returns or raises


@contextmanager
def short_lived_connection(
    path: str,
    *,
    read_only: bool = False,
    attempts: Optional[int] = None,
    delay_s: Optional[float] = None,
) -> Iterator[duckdb.DuckDBPyConnection]:
    """Hold *path* only for the duration of a ``with`` block.

    On a clean exit from a writable block the connection runs ``CHECKPOINT``,
    so the committed data is in the main database file when the block ends.
    The connection is closed on every exit path, which releases the lock.

    Args:
        path: Database file path.  ``":memory:"`` is rejected: an in-memory
            database does not survive its connection, so a short-lived one
            would silently discard everything written to it.
        read_only: Open read-only instead of read-write.
        attempts: Passed to :func:`connect_with_retry`.
        delay_s: Passed to :func:`connect_with_retry`.

    Yields:
        The open connection.

    Raises:
        ValueError: If *path* is ``":memory:"``.
    """
    if path == MEMORY_PATH:
        raise ValueError("short_lived_connection() cannot be used with ':memory:'")
    conn = connect_with_retry(path, read_only=read_only, attempts=attempts, delay_s=delay_s)
    try:
        yield conn
        if not read_only:
            conn.execute("CHECKPOINT")
    finally:
        conn.close()


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

    The store opens its connection on construction.  For a file that another
    process reads, use it as a context manager so the connection is
    checkpointed and closed — and the file lock released — as soon as the
    block ends::

        with DuckDBStore("/data/bamboo/db/jobs.duckdb") as store:
            store.write_table("t", rows)

    Attributes:
        path: Database file path, or ``":memory:"`` for an in-memory database.
    """

    def __init__(self, path: str = MEMORY_PATH) -> None:
        """Initialize the DuckDB store.

        Args:
            path: Path to the DuckDB database file.  Use ``":memory:"`` for
                an in-memory database (default).  A file that another process
                holds is retried briefly (see :func:`connect_with_retry`).
        """
        self.path = path
        self._conn: duckdb.DuckDBPyConnection
        if path == MEMORY_PATH:
            self._conn = duckdb.connect(database=path, read_only=False)
        else:
            self._conn = connect_with_retry(path, read_only=False)
        self._closed = False
        self._init_meta()

    def close(self) -> None:
        """Checkpoint (for a file-backed store) and close the connection.

        Safe to call more than once.  Any use of the store after this raises
        DuckDB's ``ConnectionException``.
        """
        if self._closed:
            return
        self._closed = True
        try:
            if self.path != MEMORY_PATH:
                self._conn.execute("CHECKPOINT")
        finally:
            self._conn.close()

    def __enter__(self) -> "DuckDBStore":
        """Return the store itself for use in a ``with`` block.

        Returns:
            This store.
        """
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        """Close the store at the end of a ``with`` block.

        Args:
            exc_type: Exception type, if the block raised.
            exc: Exception instance, if the block raised.
            tb: Traceback, if the block raised.
        """
        self.close()

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
