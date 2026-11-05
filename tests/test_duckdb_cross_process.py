"""Cross-process DuckDB tests: real readers and writers in separate processes.

DuckDB allows either one read-write process or many read-only processes on a
file, never both.  A writer that keeps its connection open therefore locks
Bamboo MCP out of ``cric.db`` and ``jobs.duckdb`` for as long as it runs.  That
was the behaviour until S-1/S-2, and no in-process test could see it, because
DuckDB's lock is per process: a second connection inside the same process
shares the first one's lock instead of conflicting with it.

Every lock assertion here therefore runs the other side in a real subprocess
(``sys.executable`` with only ``duckdb`` imported).
"""
from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import MagicMock, patch

import duckdb
import pytest

from bamboo_mcp_services.agents.cric_agent.agent import CricAgent, CricAgentConfig
from bamboo_mcp_services.agents.cric_agent.cric_fetcher import CricQueuedataFetcher
from bamboo_mcp_services.agents.ingestion_agent.agent import (
    BigPandaJobsConfig,
    IngestionAgent,
    IngestionAgentConfig,
)
from bamboo_mcp_services.agents.ingestion_agent.bigpanda_jobs_fetcher import (
    RETENTION_TABLES,
    BigPandaJobsFetcher,
)
from bamboo_mcp_services.common.panda.source import RawSnapshot
from bamboo_mcp_services.common.storage import duckdb_store
from bamboo_mcp_services.common.storage.duckdb_store import (
    DuckDBStore,
    connect_with_retry,
    is_lock_conflict,
    short_lived_connection,
)
from bamboo_mcp_services.common.storage.schema import apply_schema

REPO_ROOT = Path(__file__).resolve().parents[1]

CRIC_TWO_QUEUES: dict[str, Any] = {
    "QUEUE_A": {"status": "online", "cloud": "US", "corecount": 8},
    "QUEUE_B": {"status": "offline", "cloud": "DE", "corecount": 1},
}

BIGPANDA_PAYLOAD: dict[str, Any] = {
    "jobs": [{"pandaid": 1, "computingsite": "Q1", "jobstatus": "finished", "taskid": 7, "jeditaskid": 8}],
    "selectionsummary": [{"field": "jobstatus", "list": [{"kname": "finished", "kvalue": 1}]}],
    "errsByCount": [{"error": "pilot", "codename": "x", "codeval": 1, "count": 1, "pandalist": [1]}],
}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

class _Reader:
    """A subprocess holding a read-only connection to a DuckDB file."""

    def __init__(self, path: str, hold_s: float) -> None:
        """Start the subprocess and wait until it has the file open.

        Args:
            path: Database file.
            hold_s: Seconds the subprocess keeps the connection open.
        """
        code = textwrap.dedent(f"""
            import duckdb, sys, time
            c = duckdb.connect({path!r}, read_only=True)
            print("open", flush=True)
            time.sleep({hold_s})
            c.close()
        """)
        self.proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
        assert self.proc.stdout is not None
        line = self.proc.stdout.readline().strip()
        assert line == "open", f"reader failed to open {path}"

    def stop(self) -> None:
        """Terminate the subprocess if it is still running and reap it."""
        if self.proc.poll() is None:
            self.proc.terminate()
        self.proc.wait(timeout=10)


def _read_in_subprocess(path: str, sql: str) -> str:
    """Run *sql* in a fresh process with a read-only connection.

    Args:
        path: Database file.
        sql: Query whose first row is printed.

    Returns:
        The printed first row.

    Raises:
        AssertionError: If the subprocess fails, with its stderr.
    """
    code = f"import duckdb; print(duckdb.connect({path!r}, read_only=True).execute({sql!r}).fetchone())"
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"reader process failed: {r.stderr.strip().splitlines()[-1:]}"
    return r.stdout.strip()


def _fake_source_class(payload: dict[str, Any]) -> type:
    """Return a stand-in for ``BaseSource`` whose file fetch returns *payload*.

    Args:
        payload: Parsed CRIC JSON to return.

    Returns:
        A class with a ``fetch_from_file`` method.
    """
    text = json.dumps(payload, sort_keys=True)

    class _FakeSource:
        def fetch_from_file(self, path: str) -> RawSnapshot:
            return RawSnapshot(
                source=path,
                raw=json.loads(text),
                fetched_utc=datetime.now(timezone.utc).isoformat(),
                content_hash=hashlib.sha256(text.encode()).hexdigest(),
            )

    return _FakeSource


@pytest.fixture
def fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shorten the module-level lock retry so lock-failure tests stay fast."""
    monkeypatch.setattr(duckdb_store, "LOCK_RETRY_ATTEMPTS", 2)
    monkeypatch.setattr(duckdb_store, "LOCK_RETRY_DELAY_S", 0.05)


@pytest.fixture
def mock_bigpanda() -> Iterator[MagicMock]:
    """Patch ``requests.get`` in the BigPanda fetcher with a canned payload."""
    response = MagicMock()
    response.json.return_value = BIGPANDA_PAYLOAD
    response.raise_for_status.return_value = None
    with patch(
        "bamboo_mcp_services.agents.ingestion_agent.bigpanda_jobs_fetcher.requests.get",
        return_value=response,
    ) as m:
        yield m


def _make_db(path: Path) -> str:
    """Create a small checkpointed database file and return its path.

    Args:
        path: Where to create it.

    Returns:
        The path as a string.
    """
    c = duckdb.connect(str(path))
    c.execute("CREATE TABLE t AS SELECT range AS x FROM range(10)")
    c.close()
    return str(path)


# ─────────────────────────────────────────────────────────────────────────────
# duckdb_store helpers
# ─────────────────────────────────────────────────────────────────────────────

class TestLockHelpers:

    def test_reader_in_another_process_blocks_a_writer(self, tmp_path: Path) -> None:
        """The premise every other test relies on, checked rather than assumed."""
        db = _make_db(tmp_path / "a.db")
        reader = _Reader(db, hold_s=5)
        try:
            with pytest.raises(duckdb.IOException) as exc_info:
                duckdb.connect(db, read_only=False)
            assert is_lock_conflict(exc_info.value)
        finally:
            reader.stop()

    def test_connect_with_retry_waits_for_the_lock_to_be_released(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path / "a.db")
        reader = _Reader(db, hold_s=1.0)
        try:
            t0 = time.monotonic()
            conn = connect_with_retry(db, read_only=False, attempts=40, delay_s=0.1)
            conn.close()
            assert time.monotonic() - t0 > 0.3, "connected while the reader still held the file"
        finally:
            reader.stop()

    def test_connect_with_retry_gives_up_after_the_last_attempt(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path / "a.db")
        reader = _Reader(db, hold_s=30)
        try:
            with pytest.raises(duckdb.IOException) as exc_info:
                connect_with_retry(db, read_only=False, attempts=3, delay_s=0.05)
            assert is_lock_conflict(exc_info.value)
        finally:
            reader.stop()

    def test_errors_other_than_lock_conflicts_are_not_retried(self, tmp_path: Path) -> None:
        # "blocks" in the path: the message carries the path, so matching on
        # the bare word "lock" would misclassify this as a lock conflict.
        missing_dir = str(tmp_path / "blocks" / "no" / "such" / "dir" / "x.db")
        t0 = time.monotonic()
        with pytest.raises(duckdb.Error) as exc_info:
            connect_with_retry(missing_dir, read_only=False, attempts=10, delay_s=1.0)
        assert not is_lock_conflict(exc_info.value)
        assert time.monotonic() - t0 < 1.0, "a non-lock error was retried"

    def test_short_lived_connection_checkpoints_and_releases(self, tmp_path: Path) -> None:
        db = str(tmp_path / "s.db")
        with short_lived_connection(db) as conn:
            conn.execute("CREATE TABLE t AS SELECT range AS x FROM range(1000)")
        assert not Path(db + ".wal").exists(), "data left in the WAL — CHECKPOINT did not run"
        assert _read_in_subprocess(db, "SELECT COUNT(*) FROM t") == "(1000,)"

    def test_checkpoint_does_not_rely_on_being_the_last_connection(self, tmp_path: Path) -> None:
        """Closing a connection checkpoints only if no other connection in the process shares the database.

        Without the explicit CHECKPOINT, the write below stays in the WAL.
        """
        db = _make_db(tmp_path / "shared.db")
        other = duckdb.connect(db)   # same process: shares the database instance
        try:
            with short_lived_connection(db) as conn:
                conn.execute("CREATE TABLE u AS SELECT range AS r FROM range(200000)")
            wal = Path(db + ".wal")
            assert not wal.exists() or wal.stat().st_size == 0, "write left in the WAL"
        finally:
            other.close()

    def test_short_lived_connection_releases_after_an_exception(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path / "e.db")
        with pytest.raises(RuntimeError):
            with short_lived_connection(db):
                raise RuntimeError("boom")
        assert _read_in_subprocess(db, "SELECT COUNT(*) FROM t") == "(10,)"

    def test_short_lived_connection_rejects_memory(self) -> None:
        with pytest.raises(ValueError):
            with short_lived_connection(":memory:"):
                pass

    def test_store_used_as_context_manager_releases_the_file(self, tmp_path: Path) -> None:
        db = str(tmp_path / "store.db")
        with DuckDBStore(db) as store:
            store.write_table("t", [{"x": 1}])
        assert _read_in_subprocess(db, "SELECT COUNT(*) FROM t") == "(1,)"

    def test_store_close_is_idempotent(self, tmp_path: Path) -> None:
        store = DuckDBStore(str(tmp_path / "c.db"))
        store.close()
        store.close()


# ─────────────────────────────────────────────────────────────────────────────
# CRIC agent (S-1)
# ─────────────────────────────────────────────────────────────────────────────

class TestCricHoldsTheFileOnlyWhileLoading:

    def test_fetcher_needs_exactly_one_of_conn_and_path(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            CricQueuedataFetcher(conn=None, cric_path="/x")
        with pytest.raises(ValueError):
            CricQueuedataFetcher(conn=duckdb.connect(), cric_path="/x", duckdb_path=str(tmp_path / "c.db"))

    def test_running_agent_does_not_lock_readers_out(self, tmp_path: Path) -> None:
        """The aipanda033 failure: a running cric daemon made cric.db unreadable."""
        db = str(tmp_path / "cric.db")
        agent = CricAgent(config=CricAgentConfig(cric_path="/fake", duckdb_path=db, refresh_interval_s=0))
        with patch("bamboo_mcp_services.agents.cric_agent.cric_fetcher.BaseSource", _fake_source_class(CRIC_TWO_QUEUES)):
            agent.start()
            agent.tick()
        try:
            assert agent.state.value == "running"
            assert _read_in_subprocess(db, "SELECT COUNT(*) FROM queuedata") == "(2,)"
            assert not Path(db + ".wal").exists(), "load left in the WAL"
        finally:
            agent.stop()

    def test_locked_file_defers_the_load_to_the_next_tick(
        self, tmp_path: Path, fast_retries: None, caplog: pytest.LogCaptureFixture,
    ) -> None:
        db = _make_db(tmp_path / "cric.db")
        fetcher = CricQueuedataFetcher(conn=None, cric_path="/fake", refresh_interval_s=600, duckdb_path=db)
        reader = _Reader(db, hold_s=30)
        try:
            with patch("bamboo_mcp_services.agents.cric_agent.cric_fetcher.BaseSource", _fake_source_class(CRIC_TWO_QUEUES)), \
                    caplog.at_level(logging.ERROR):
                assert fetcher.run_cycle() is False
            assert "locked by another process" in caplog.text
            assert fetcher._last_hash is None, "hash recorded for a load that did not happen"
            assert fetcher._last_attempt is None, "lock failure should retry at the next tick, not after refresh_interval_s"
        finally:
            reader.stop()

        with patch("bamboo_mcp_services.agents.cric_agent.cric_fetcher.BaseSource", _fake_source_class(CRIC_TWO_QUEUES)):
            assert fetcher.run_cycle() is True
        assert _read_in_subprocess(db, "SELECT COUNT(*) FROM queuedata") == "(2,)"

    def test_first_load_is_not_skipped_shortly_after_boot(self, tmp_path: Path) -> None:
        """monotonic() counts from boot; a 0.0 'never ran' sentinel skipped the first load after a reboot."""
        db = str(tmp_path / "cric.db")
        fetcher = CricQueuedataFetcher(conn=None, cric_path="/fake", refresh_interval_s=600, duckdb_path=db)
        with patch("bamboo_mcp_services.agents.cric_agent.cric_fetcher.time.monotonic", return_value=120.0), \
                patch("bamboo_mcp_services.agents.cric_agent.cric_fetcher.BaseSource", _fake_source_class(CRIC_TWO_QUEUES)):
            assert fetcher.run_cycle() is True

    def test_agent_start_fails_loudly_when_the_file_cannot_be_opened(self, tmp_path: Path) -> None:
        agent = CricAgent(config=CricAgentConfig(cric_path="/fake", duckdb_path=str(tmp_path / "missing" / "cric.db")))
        with pytest.raises(duckdb.Error):
            agent.start()


# ─────────────────────────────────────────────────────────────────────────────
# Ingestion agent (S-2)
# ─────────────────────────────────────────────────────────────────────────────

class TestIngestionHoldsTheFileOnlyPerQueueWrite:

    def test_fetcher_needs_exactly_one_of_conn_and_path(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            BigPandaJobsFetcher(conn=None)
        with pytest.raises(ValueError):
            BigPandaJobsFetcher(conn=duckdb.connect(), duckdb_path=str(tmp_path / "j.db"))

    def test_running_agent_does_not_lock_readers_out(self, tmp_path: Path, mock_bigpanda: MagicMock) -> None:
        db = str(tmp_path / "jobs.duckdb")
        cfg = IngestionAgentConfig(
            sources=[],
            duckdb_path=db,
            bigpanda_jobs=BigPandaJobsConfig(queues=["Q1", "Q2"], cycle_interval_s=0, inter_queue_delay_s=0),
        )
        agent = IngestionAgent(config=cfg)
        agent.start()
        try:
            agent.tick_once()
            assert agent.state.value == "running"
            assert agent.store is None, "a file-backed agent must not hold a store"
            assert _read_in_subprocess(db, "SELECT COUNT(*) FROM jobs") == "(1,)"
            assert not Path(db + ".wal").exists(), "write left in the WAL"
        finally:
            agent.stop()

    def test_file_is_free_during_the_inter_queue_sleep(self, tmp_path: Path, mock_bigpanda: MagicMock) -> None:
        """A cycle over ~230 queues lasts hours; the file must be readable between queues."""
        db = str(tmp_path / "jobs.duckdb")
        fetcher = BigPandaJobsFetcher(conn=None, queues=["Q1", "Q2"], cycle_interval_s=0, inter_queue_delay_s=0, duckdb_path=db)
        seen: list[str] = []
        with patch.object(fetcher, "_interruptible_sleep", side_effect=lambda _s: seen.append(_read_in_subprocess(db, "SELECT COUNT(*) FROM jobs"))):
            fetcher.run_cycle(force=True)
        assert seen == ["(1,)"]

    def test_generic_sources_use_short_lived_stores(self, tmp_path: Path) -> None:
        from bamboo_mcp_services.agents.ingestion_agent.agent import SourceConfig

        src = tmp_path / "q.json"
        src.write_text('{"queues": []}')
        db = str(tmp_path / "jobs.duckdb")
        cfg = IngestionAgentConfig(
            sources=[SourceConfig(name="t1", type="cric", mode="file", path=str(src), interval_s=0)],
            duckdb_path=db,
            bigpanda_jobs=BigPandaJobsConfig(enabled=False),
        )
        agent = IngestionAgent(config=cfg)
        agent.start()
        try:
            agent.tick()
            assert _read_in_subprocess(db, "SELECT COUNT(*) FROM snapshots WHERE ok") == "(1,)"
            assert _read_in_subprocess(db, "SELECT COUNT(*) FROM t1_history") == "(1,)"
        finally:
            agent.stop()


# ─────────────────────────────────────────────────────────────────────────────
# Retention (S-4)
# ─────────────────────────────────────────────────────────────────────────────

def _seed_retention_rows(conn: duckdb.DuckDBPyConnection, now: datetime) -> None:
    """Insert one row per retention table at each of three ages: 1 day, 7 days exactly, 8 days.

    Args:
        conn: Connection with the schema applied.
        now: Reference time.
    """
    fmt = "%Y-%m-%d %H:%M:%S"
    for i, age in enumerate((timedelta(days=1), timedelta(days=7), timedelta(days=8))):
        ts = (now - age).strftime(fmt)
        conn.execute("INSERT INTO jobs (pandaid, _queue, _fetched_utc) VALUES (?, 'Q', ?)", [i, ts])
        conn.execute("INSERT INTO selectionsummary (id, field, list_json, _queue, _fetched_utc) VALUES (?, 'f', '[]', 'Q', ?)", [i, ts])
        conn.execute("INSERT INTO errors_by_count (id, _queue, _fetched_utc) VALUES (?, 'Q', ?)", [i, ts])


class TestRetention:

    NOW = datetime(2026, 11, 4, 12, 0, 0, tzinfo=timezone.utc)

    def test_purge_deletes_only_rows_older_than_the_cutoff(self) -> None:
        conn = duckdb.connect()
        apply_schema(conn)
        _seed_retention_rows(conn, self.NOW)
        fetcher = BigPandaJobsFetcher(conn=conn, retention_days=7)

        deleted = fetcher.purge_expired(now_utc=self.NOW)

        assert deleted == {t: 1 for t in RETENTION_TABLES}
        for table, key in (("jobs", "pandaid"), ("selectionsummary", "id"), ("errors_by_count", "id")):
            kept = sorted(r[0] for r in conn.execute(f"SELECT {key} FROM {table}").fetchall())
            assert kept == [0, 1], f"{table}: the row exactly 7 days old must survive, the 8-day row must not"

    def test_retention_zero_purges_nothing(self) -> None:
        conn = duckdb.connect()
        apply_schema(conn)
        _seed_retention_rows(conn, self.NOW)
        fetcher = BigPandaJobsFetcher(conn=conn, retention_days=0)
        assert fetcher.purge_expired(now_utc=self.NOW) == {}
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone() == (3,)

    def test_negative_retention_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            BigPandaJobsFetcher(conn=duckdb.connect(), retention_days=-1)

    def test_cycle_purges_before_fetching(self, mock_bigpanda: MagicMock) -> None:
        fetcher = BigPandaJobsFetcher(conn=duckdb.connect(), queues=["Q1"], cycle_interval_s=0, retention_days=7)
        order: list[str] = []
        with patch.object(fetcher, "purge_expired", side_effect=lambda **_: order.append("purge") or {}), \
                patch.object(fetcher, "_fetch_and_persist", side_effect=lambda *_: order.append("fetch")):
            fetcher.run_cycle(force=True)
        assert order == ["purge", "fetch"]

    def test_failed_purge_does_not_stop_the_cycle(self, mock_bigpanda: MagicMock) -> None:
        conn = duckdb.connect()
        fetcher = BigPandaJobsFetcher(conn=conn, queues=["Q1"], cycle_interval_s=0, retention_days=7)
        with patch.object(fetcher, "purge_expired", side_effect=RuntimeError("purge broke")):
            assert fetcher.run_cycle(force=True) is True
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone() == (1,)

    def test_retention_days_reaches_the_fetcher_through_the_cli_overrides(self, tmp_path: Path) -> None:
        """--max-queues used to rebuild the config field by field, dropping new fields."""
        from bamboo_mcp_services.agents.ingestion_agent import cli

        captured: dict[str, Any] = {}
        cfg = tmp_path / "ingestion.yaml"
        cfg.write_text(
            "bigpanda_jobs:\n  enabled: true\n  queues: [Q1]\n  retention_days: 7\n"
            f"duckdb_path: {tmp_path / 'jobs.duckdb'}\n"
        )

        def _capture(self: IngestionAgent, name: str = "x", config: Any = None) -> None:
            captured["cfg"] = config
            raise SystemExit(0)

        with patch.object(IngestionAgent, "__init__", _capture), pytest.raises(SystemExit):
            cli.main(["--config", str(cfg), "--max-queues", "1", "--inter-queue-delay", "0", "--log-file", ""])
        assert captured["cfg"].bigpanda_jobs.retention_days == 7
        assert captured["cfg"].bigpanda_jobs.max_queues == 1


# ─────────────────────────────────────────────────────────────────────────────
# scripts/compact_duckdb.py
# ─────────────────────────────────────────────────────────────────────────────

def _run_compact(*args: str) -> subprocess.CompletedProcess:
    """Run the compaction script as an operator would.

    Args:
        *args: Script arguments.

    Returns:
        The completed process.
    """
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "compact_duckdb.py"), *args],
        capture_output=True, text=True, timeout=120,
    )


class TestCompactScript:

    def test_copy_has_the_same_rows_and_is_smaller(self, tmp_path: Path) -> None:
        src = str(tmp_path / "big.db")
        c = duckdb.connect(src)
        c.execute("CREATE TABLE t AS SELECT range AS id, md5(range::VARCHAR) AS h FROM range(300000)")
        c.execute("DELETE FROM t WHERE id >= 30000")
        c.close()

        r = _run_compact(src, str(tmp_path / "small.db"))

        assert r.returncode == 0, r.stderr
        assert _read_in_subprocess(str(tmp_path / "small.db"), "SELECT COUNT(*) FROM t") == "(30000,)"
        assert (tmp_path / "small.db").stat().st_size < Path(src).stat().st_size
        assert Path(src).exists(), "the script must never touch the source"

    def test_refuses_to_overwrite_the_destination(self, tmp_path: Path) -> None:
        src = _make_db(tmp_path / "a.db")
        dst = tmp_path / "b.db"
        dst.write_text("precious")
        r = _run_compact(src, str(dst))
        assert r.returncode == 1
        assert dst.read_text() == "precious"

    def test_fails_while_a_writer_holds_the_source(self, tmp_path: Path) -> None:
        src = _make_db(tmp_path / "a.db")
        writer = subprocess.Popen(
            [sys.executable, "-c", f"import duckdb,time; c=duckdb.connect({src!r}); print('open', flush=True); time.sleep(30)"],
            stdout=subprocess.PIPE, text=True,
        )
        try:
            assert writer.stdout is not None and writer.stdout.readline().strip() == "open"
            r = _run_compact(src, str(tmp_path / "b.db"))
            assert r.returncode == 1
            assert "still running" in r.stderr
        finally:
            writer.terminate()
            writer.wait(timeout=10)
