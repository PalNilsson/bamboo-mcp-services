#!/usr/bin/env python3
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

"""Copy a DuckDB database into a fresh file, reclaiming space freed by deletes.

DuckDB reuses the space that ``DELETE`` frees, but it returns space to the
filesystem only when the free blocks are at the end of the file.  A routine
retention purge frees blocks scattered through the file, so ``jobs.duckdb``
stops growing but does not get smaller.  Copying the database into a new file
writes only the live rows.

The script never touches the source and never replaces it.  It writes
``DST``, checks that every table has the same row count in both files, and
prints the commands for swapping the files, which the operator runs.

Stop every process that writes the source first.  The source is opened
read-only, so a running writer makes the script fail with a lock error rather
than copy a moving target.

Usage::

    python scripts/compact_duckdb.py /data/bamboo/db/jobs.duckdb /data/bamboo/db/jobs.compact.duckdb

Requires DuckDB 0.10 or later (``COPY FROM DATABASE``).
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Optional, Sequence

import duckdb


def _quote(path: str) -> str:
    """Return *path* as a single-quoted SQL string literal.

    Args:
        path: Filesystem path.

    Returns:
        The path with embedded single quotes doubled, wrapped in quotes.
    """
    return "'" + path.replace("'", "''") + "'"


def _row_counts(conn: duckdb.DuckDBPyConnection, catalog: str) -> dict[str, int]:
    """Count rows in every base table of an attached database.

    Args:
        conn: Connection with *catalog* attached.
        catalog: Alias under which the database is attached.

    Returns:
        ``{"schema.table": row_count}``.
    """
    tables = conn.execute(
        "SELECT table_schema, table_name FROM information_schema.tables "
        "WHERE table_catalog = ? AND table_type = 'BASE TABLE' ORDER BY 1, 2",
        [catalog],
    ).fetchall()
    counts: dict[str, int] = {}
    for schema, table in tables:
        row = conn.execute(f'SELECT COUNT(*) FROM "{catalog}"."{schema}"."{table}"').fetchone()
        counts[f"{schema}.{table}"] = int(row[0]) if row else 0
    return counts


def compact(src: str, dst: str) -> int:
    """Copy *src* into the new file *dst* and verify the copy.

    Args:
        src: Existing database file.  Opened read-only.
        dst: Path for the new file.  Must not exist.

    Returns:
        Process exit code: 0 on success, 1 on any failure.
    """
    if not os.path.isfile(src):
        print(f"ERROR: source {src} does not exist", file=sys.stderr)
        return 1
    if os.path.exists(dst):
        print(f"ERROR: destination {dst} already exists — refusing to overwrite", file=sys.stderr)
        return 1

    conn = duckdb.connect()
    try:
        conn.execute(f"ATTACH {_quote(src)} AS src (READ_ONLY)")
        conn.execute(f"ATTACH {_quote(dst)} AS dst")
        conn.execute("COPY FROM DATABASE src TO dst")
        src_counts = _row_counts(conn, "src")
        dst_counts = _row_counts(conn, "dst")
        conn.execute("DETACH dst")
        conn.execute("DETACH src")
    except duckdb.Error as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        print("Is a writer (bamboo-ingestion, bamboo-cric) still running against the source?", file=sys.stderr)
        return 1
    finally:
        conn.close()

    if src_counts != dst_counts:
        print("ERROR: row counts differ — the copy is NOT safe to use:", file=sys.stderr)
        for name in sorted(set(src_counts) | set(dst_counts)):
            print(f"  {name}: source={src_counts.get(name)} copy={dst_counts.get(name)}", file=sys.stderr)
        return 1

    mb = 1024 * 1024
    print(f"Copied {len(src_counts)} table(s); row counts match:")
    for name, n in src_counts.items():
        print(f"  {name}: {n}")
    print(f"Size: {os.path.getsize(src) / mb:.1f} MiB -> {os.path.getsize(dst) / mb:.1f} MiB")
    print("\nTo switch over, with every writer and reader stopped:")
    print(f"  mv {src} {src}.old && mv {dst} {src}")
    print(f"Delete {src}.old once the services have run against the new file.")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Parse arguments and run :func:`compact`.

    Args:
        argv: Command-line arguments.  ``None`` uses ``sys.argv``.

    Returns:
        Process exit code.
    """
    p = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    p.add_argument("src", help="existing DuckDB file (opened read-only)")
    p.add_argument("dst", help="new file to create (must not exist)")
    args = p.parse_args(argv)
    return compact(args.src, args.dst)


if __name__ == "__main__":
    raise SystemExit(main())
