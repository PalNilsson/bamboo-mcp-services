"""Tests for atomic update behaviour across DuckDB and ChromaDB storage layers.

Covers:
- :func:`~bamboo_mcp_services.common.storage.duckdb_store.atomic_swap_table`:
  first-run (no live table), normal swap, crash-recovery (stale retiring table),
  rollback on error.
- :class:`~bamboo_mcp_services.common.storage.duckdb_store.DuckDBStore`
  ``write_table(overwrite=True)``: reader never sees a gap between the old and
  new versions.
- :class:`~bamboo_mcp_services.agents.cric_agent.cric_fetcher.CricQueuedataFetcher`
  ``_load``: reader thread always observes a non-zero row count during a
  concurrent refresh.
- :class:`~bamboo_mcp_services.agents.document_monitor_agent.storage.CollectionRouter`:
  live/idle slot resolution, first-run default, commit_swap routing update,
  sidecar atomicity.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any
from unittest.mock import MagicMock

import duckdb
import pytest

from bamboo_mcp_services.common.storage.duckdb_store import (
    DuckDBStore,
    atomic_swap_table,
)
from bamboo_mcp_services.agents.cric_agent.cric_fetcher import CricQueuedataFetcher
from bamboo_mcp_services.agents.document_monitor_agent.storage import (
    ChromaWrapper,
    CollectionRouter,
)


# ===========================================================================
# atomic_swap_table
# ===========================================================================


class TestAtomicSwapTable:
    """Unit tests for the atomic_swap_table free function."""

    def _conn(self) -> duckdb.DuckDBPyConnection:
        return duckdb.connect(":memory:")

    def test_first_run_no_live_table(self):
        """swap when live table does not yet exist: staging is simply renamed."""
        conn = self._conn()
        conn.execute("CREATE TABLE staging (x INT)")
        conn.execute("INSERT INTO staging VALUES (42)")

        atomic_swap_table(conn, "staging", "live")

        rows = conn.execute("SELECT x FROM live").fetchall()
        assert rows == [(42,)]

        # staging no longer exists
        tables = [r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables"
        ).fetchall()]
        assert "staging" not in tables

    def test_normal_swap_replaces_live(self):
        """swap promotes staging and removes old live."""
        conn = self._conn()
        conn.execute("CREATE TABLE live (x INT)")
        conn.execute("INSERT INTO live VALUES (1)")
        conn.execute("CREATE TABLE staging (x INT)")
        conn.execute("INSERT INTO staging VALUES (99)")

        atomic_swap_table(conn, "staging", "live")

        rows = conn.execute("SELECT x FROM live").fetchall()
        assert rows == [(99,)]

        tables = [r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables"
        ).fetchall()]
        assert "staging" not in tables
        assert "live_retiring" not in tables

    def test_stale_retiring_table_is_cleaned_up(self):
        """A leftover retiring table from a previous crash does not block the swap."""
        conn = self._conn()
        conn.execute("CREATE TABLE live (x INT)")
        conn.execute("INSERT INTO live VALUES (7)")
        # Simulate a crash that left a retiring table behind.
        conn.execute("CREATE TABLE live_retiring (x INT)")
        conn.execute("INSERT INTO live_retiring VALUES (0)")
        conn.execute("CREATE TABLE staging (x INT)")
        conn.execute("INSERT INTO staging VALUES (55)")

        # Should not raise despite live_retiring already existing.
        atomic_swap_table(conn, "staging", "live")

        rows = conn.execute("SELECT x FROM live").fetchall()
        assert rows == [(55,)]

    def test_rollback_on_error_preserves_live(self):
        """If the swap transaction fails mid-way, the live table is unchanged."""
        conn = self._conn()
        conn.execute("CREATE TABLE live (x INT)")
        conn.execute("INSERT INTO live VALUES (11)")

        # Intentionally do NOT create the staging table — the rename should fail.
        with pytest.raises(Exception):
            atomic_swap_table(conn, "nonexistent_staging", "live")

        # Live table must still contain its original data.
        rows = conn.execute("SELECT x FROM live").fetchall()
        assert rows == [(11,)]

    def test_multiple_consecutive_swaps(self):
        """Repeated swap calls keep producing the correct live content."""
        conn = self._conn()
        for i in range(1, 4):
            conn.execute("DROP TABLE IF EXISTS staging")
            conn.execute("CREATE TABLE staging (v INT)")
            conn.execute(f"INSERT INTO staging VALUES ({i * 10})")
            atomic_swap_table(conn, "staging", "live")
            result = conn.execute("SELECT v FROM live").fetchone()[0]
            assert result == i * 10


# ===========================================================================
# DuckDBStore.write_table(overwrite=True)
# ===========================================================================


class TestDuckDBStoreWriteTableAtomic:
    """write_table(overwrite=True) must never expose a gap to readers."""

    def test_overwrite_swaps_atomically(self):
        store = DuckDBStore(":memory:")
        store.write_table("t", [{"k": "old"}], overwrite=True)
        old = store._conn.execute("SELECT data FROM t").fetchall()
        assert len(old) == 1
        assert "old" in old[0][0]

        store.write_table("t", [{"k": "new1"}, {"k": "new2"}], overwrite=True)
        rows = store._conn.execute("SELECT data FROM t").fetchall()
        assert len(rows) == 2

    def test_reader_never_sees_empty_table(self, tmp_path):
        """A background reader sees either all-old or all-new rows, never zero.

        DuckDB does not allow safe concurrent use of a single connection from
        multiple threads.  This test uses a file-backed database and a dedicated
        read connection to simulate a real concurrent reader.
        """
        import duckdb as _duckdb

        db_path = str(tmp_path / "test.duckdb")
        wconn = _duckdb.connect(db_path)
        rconn = _duckdb.connect(db_path)

        # Seed initial data via write connection directly (bypass DuckDBStore).
        wconn.execute("CREATE TABLE readings (data JSON, updated_utc TIMESTAMP)")
        for i in range(50):
            wconn.execute("INSERT INTO readings VALUES ('{\"v\":0}', NOW())")

        seen_zero = threading.Event()
        stop = threading.Event()
        read_errors: list[str] = []

        def reader():
            while not stop.is_set():
                try:
                    count = rconn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
                    if count == 0:
                        seen_zero.set()
                        read_errors.append("saw 0 rows during swap")
                except Exception as exc:
                    read_errors.append(str(exc))
                time.sleep(0.0005)

        t = threading.Thread(target=reader, daemon=True)
        t.start()

        store = DuckDBStore.__new__(DuckDBStore)
        store.path = db_path
        store._conn = wconn

        for _ in range(5):
            new_rows = [{"v": i + 1000} for i in range(50)]
            store.write_table("readings", new_rows, overwrite=True)

        stop.set()
        t.join(timeout=2)
        rconn.close()
        wconn.close()

        assert not read_errors, f"Reader errors: {read_errors}"
        assert not seen_zero.is_set(), "Reader saw 0 rows during a swap"

    def test_append_mode_does_not_drop(self):
        """overwrite=False appends without clearing existing rows."""
        store = DuckDBStore(":memory:")
        store.write_table("log", [{"a": 1}], overwrite=False)
        store.write_table("log", [{"a": 2}], overwrite=False)
        rows = store._conn.execute("SELECT COUNT(*) FROM log").fetchone()[0]
        assert rows == 2


# ===========================================================================
# CricQueuedataFetcher — reader continuity during refresh
# ===========================================================================


class TestCricFetcherAtomicRefresh:
    """The queuedata table must remain readable throughout a _load() call."""

    def _make_fetcher(self) -> tuple[duckdb.DuckDBPyConnection, CricQueuedataFetcher]:
        conn = duckdb.connect(":memory:")
        fetcher = CricQueuedataFetcher(
            conn=conn,
            cric_path="/nonexistent",
            refresh_interval_s=0,
        )
        return conn, fetcher

    def _queue_payload(self, name: str, value: int) -> dict[str, Any]:
        return {name: {"corecount": value, "cloud": "US", "status": "online"}}

    def test_first_load_creates_table(self):
        conn, fetcher = self._make_fetcher()
        count = fetcher._load(self._queue_payload("Q1", 8))
        assert count == 1
        rows = conn.execute("SELECT queue FROM queuedata").fetchall()
        assert rows == [("Q1",)]

    def test_reload_replaces_content(self):
        conn, fetcher = self._make_fetcher()
        fetcher._load(self._queue_payload("Q1", 8))
        fetcher._load({"Q2": {"corecount": 4, "cloud": "EU", "status": "test"}})
        rows = conn.execute("SELECT queue FROM queuedata").fetchall()
        queues = [r[0] for r in rows]
        assert "Q1" not in queues
        assert "Q2" in queues

    def test_reader_never_sees_zero_rows_during_reload(self, tmp_path):
        """A concurrent reader must not observe an empty queuedata table.

        Uses a file-backed DuckDB database with a dedicated read connection
        because DuckDB's single-connection model is not thread-safe.
        """
        import duckdb as _duckdb

        db_path = str(tmp_path / "cric.duckdb")
        wconn = _duckdb.connect(db_path)
        rconn = _duckdb.connect(db_path)

        fetcher = CricQueuedataFetcher(
            conn=wconn,
            cric_path="/nonexistent",
            refresh_interval_s=0,
        )
        # Seed initial data.
        fetcher._load({f"INIT_{i}": {"corecount": i} for i in range(20)})

        seen_zero = threading.Event()
        stop = threading.Event()
        errors: list[str] = []

        def reader():
            while not stop.is_set():
                try:
                    n = rconn.execute("SELECT COUNT(*) FROM queuedata").fetchone()[0]
                    if n == 0:
                        seen_zero.set()
                        errors.append("saw 0 rows")
                except Exception as exc:
                    errors.append(str(exc))
                time.sleep(0.0005)

        t = threading.Thread(target=reader, daemon=True)
        t.start()

        for cycle in range(6):
            data = {f"Q_{cycle}_{i}": {"corecount": i} for i in range(20)}
            fetcher._load(data)

        stop.set()
        t.join(timeout=2)
        rconn.close()
        wconn.close()

        assert not errors, f"Reader errors: {errors}"
        assert not seen_zero.is_set(), "Reader saw 0 rows during a _load()"

    def test_no_staging_table_left_after_load(self):
        """queuedata_staging must be cleaned up after a successful _load."""
        conn, fetcher = self._make_fetcher()
        fetcher._load(self._queue_payload("Q1", 8))

        tables = [r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables"
        ).fetchall()]
        assert "queuedata_staging" not in tables

    def test_load_empty_data_leaves_existing_table_intact(self):
        """Passing an empty dict must not wipe the existing table."""
        conn, fetcher = self._make_fetcher()
        fetcher._load(self._queue_payload("Q1", 8))
        count_before = conn.execute("SELECT COUNT(*) FROM queuedata").fetchone()[0]

        result = fetcher._load({})
        assert result == 0
        count_after = conn.execute("SELECT COUNT(*) FROM queuedata").fetchone()[0]
        assert count_after == count_before


# ===========================================================================
# CollectionRouter
# ===========================================================================


class TestCollectionRouter:
    """Unit tests for the JSON-sidecar blue/green router."""

    def test_first_access_defaults_to_slot_a(self, tmp_path):
        sidecar = str(tmp_path / "routing.json")
        router = CollectionRouter(sidecar)
        assert router.live_name("docs") == "docs__a"

    def test_first_access_writes_sidecar(self, tmp_path):
        sidecar = tmp_path / "routing.json"
        router = CollectionRouter(str(sidecar))
        router.live_name("docs")
        assert sidecar.exists()
        data = json.loads(sidecar.read_text())
        assert data["docs"] == "docs__a"

    def test_idle_name_is_other_slot(self, tmp_path):
        sidecar = str(tmp_path / "routing.json")
        router = CollectionRouter(sidecar)
        # Initially live=__a, so idle should be __b.
        assert router.live_name("docs") == "docs__a"
        assert router.idle_name("docs") == "docs__b"

    def test_commit_swap_updates_live(self, tmp_path):
        sidecar = str(tmp_path / "routing.json")
        router = CollectionRouter(sidecar)
        assert router.live_name("docs") == "docs__a"

        mock_chroma = MagicMock(spec=ChromaWrapper)
        router.commit_swap("docs", mock_chroma)

        assert router.live_name("docs") == "docs__b"
        # idle is now __a
        assert router.idle_name("docs") == "docs__a"

    def test_commit_swap_deletes_old_live(self, tmp_path):
        sidecar = str(tmp_path / "routing.json")
        router = CollectionRouter(sidecar)
        router.live_name("docs")  # initialise to __a

        mock_chroma = MagicMock(spec=ChromaWrapper)
        router.commit_swap("docs", mock_chroma)

        mock_chroma.delete_collection.assert_called_once_with("docs__a")

    def test_commit_swap_alternates_on_repeated_calls(self, tmp_path):
        sidecar = str(tmp_path / "routing.json")
        router = CollectionRouter(sidecar)
        mock_chroma = MagicMock(spec=ChromaWrapper)

        expected_live = ["docs__a", "docs__b", "docs__a", "docs__b"]
        for expected in expected_live:
            assert router.live_name("docs") == expected
            router.commit_swap("docs", mock_chroma)

    def test_sidecar_persists_across_instances(self, tmp_path):
        """A new CollectionRouter instance reads the sidecar left by a previous one."""
        sidecar = str(tmp_path / "routing.json")
        router1 = CollectionRouter(sidecar)
        mock_chroma = MagicMock(spec=ChromaWrapper)
        router1.commit_swap("docs", mock_chroma)  # live → __b

        router2 = CollectionRouter(sidecar)
        assert router2.live_name("docs") == "docs__b"

    def test_sidecar_write_is_atomic(self, tmp_path):
        """The sidecar is written via os.replace; the .tmp file must not linger."""
        sidecar = tmp_path / "routing.json"
        router = CollectionRouter(str(sidecar))
        router.live_name("docs")

        tmp = sidecar.with_suffix(".tmp")
        assert not tmp.exists(), ".tmp file should have been replaced (not left behind)"

    def test_corrupt_sidecar_starts_fresh(self, tmp_path):
        sidecar = tmp_path / "routing.json"
        sidecar.write_text("{ invalid json !!!", encoding="utf-8")

        router = CollectionRouter(str(sidecar))
        # Should not raise; falls back to empty state.
        assert router.live_name("docs") == "docs__a"

    def test_multiple_logical_names_are_independent(self, tmp_path):
        sidecar = str(tmp_path / "routing.json")
        router = CollectionRouter(sidecar)
        mock_chroma = MagicMock(spec=ChromaWrapper)

        # Swap only "alpha"; "beta" should remain at __a.
        router.live_name("alpha")
        router.live_name("beta")
        router.commit_swap("alpha", mock_chroma)

        assert router.live_name("alpha") == "alpha__b"
        assert router.live_name("beta") == "beta__a"


# ===========================================================================
# ChromaWrapper.create_collection — always brand-new (dimension-mismatch guard)
# ===========================================================================


class TestChromaWrapperCreateCollection:
    """create_collection must always produce a fresh collection with no inherited
    embedding dimension, so that a changed embedder model never causes a
    dimension-mismatch error on the idle slot."""

    def test_create_collection_calls_client_create_not_get_or_create(self):
        """create_collection must delegate to client.create_collection, not
        client.get_or_create_collection."""
        mock_client = MagicMock()
        mock_client.create_collection.return_value = MagicMock()

        wrapper = ChromaWrapper.__new__(ChromaWrapper)
        wrapper.client = mock_client

        wrapper.create_collection("my_col")

        mock_client.create_collection.assert_called_once_with("my_col")
        mock_client.get_or_create_collection.assert_not_called()

    def test_get_or_create_collection_does_not_call_create(self):
        """get_or_create_collection must delegate to client.get_or_create_collection
        and must NOT call client.create_collection (used for the live slot on
        startup where we want to reattach to an existing collection)."""
        mock_client = MagicMock()
        mock_client.get_or_create_collection.return_value = MagicMock()

        wrapper = ChromaWrapper.__new__(ChromaWrapper)
        wrapper.client = mock_client

        wrapper.get_or_create_collection("my_col")

        mock_client.get_or_create_collection.assert_called_once_with("my_col")
        mock_client.create_collection.assert_not_called()

    def test_idle_slot_managed_by_tick_not_ingest_file(self):
        """Slot lifecycle (delete+create+swap) is owned by _tick_impl, not _ingest_file.

        _ingest_file is now a pure writer: it receives a target collection and
        writes into it without touching slot routing.  The delete-before-create
        and commit_swap happen exactly once per cycle in _tick_impl, regardless
        of how many files are processed.
        """
        from bamboo_mcp_services.agents.document_monitor_agent.agent import (
            DocumentMonitorAgent,
        )

        call_order: list[str] = []

        mock_chroma = MagicMock(spec=ChromaWrapper)
        mock_chroma.delete_collection.side_effect = lambda name: call_order.append(f"delete:{name}")
        mock_chroma.create_collection.side_effect = lambda name: (
            call_order.append(f"create:{name}") or MagicMock()
        )
        mock_chroma.get_or_create_collection.return_value = MagicMock()
        mock_chroma.add_documents.return_value = None
        mock_chroma.persist.return_value = None

        mock_router = MagicMock()
        mock_router.live_name.return_value = "docs__a"
        mock_router.idle_name.return_value = "docs__b"

        mock_embedder = MagicMock()
        mock_embedder.encode.return_value = [[0.1, 0.2, 0.3]]

        mock_checkpoint = MagicMock()
        mock_checkpoint._data = {"processed": {}}
        mock_checkpoint.mark_processed.return_value = None

        agent = DocumentMonitorAgent.__new__(DocumentMonitorAgent)
        agent._logical_name = "docs"
        agent.chroma = mock_chroma
        agent.router = mock_router
        agent.collection = MagicMock()
        agent._embedder = mock_embedder
        agent.chunk_size = 500
        agent.chunk_overlap = 50
        agent.checkpoint = mock_checkpoint
        agent._last_processed_file = None
        agent._last_error = None

        # _ingest_file now receives the target collection from _tick_impl.
        # It must NOT call delete_collection or create_collection itself.
        target_col = MagicMock()
        agent._ingest_file("/some/file.md", "hello world " * 20, "abc123", target_col)

        # _ingest_file must not touch slot management at all.
        assert call_order == [], (
            f"_ingest_file should not call delete/create; got: {call_order}"
        )
        # It must write into the target collection it was given.
        mock_chroma.add_documents.assert_called_once()
        assert mock_chroma.add_documents.call_args[0][0] is target_col

    def test_tick_impl_deletes_and_creates_idle_slot_once_per_cycle(self, tmp_path):
        """_tick_impl deletes and creates the idle slot exactly once per cycle,
        regardless of how many files are processed, and promotes once at the end."""
        from bamboo_mcp_services.agents.document_monitor_agent.agent import (
            DocumentMonitorAgent,
        )

        call_order: list[str] = []
        idle_col = MagicMock()

        mock_chroma = MagicMock(spec=ChromaWrapper)
        mock_chroma.delete_collection.side_effect = lambda name: call_order.append(f"delete:{name}")
        mock_chroma.create_collection.side_effect = lambda name: (
            call_order.append(f"create:{name}") or idle_col
        )
        mock_chroma.get_or_create_collection.return_value = MagicMock()
        mock_chroma.add_documents.return_value = None
        mock_chroma.persist.return_value = None

        mock_router = MagicMock()
        mock_router.live_name.return_value = "docs__a"
        mock_router.idle_name.return_value = "docs__b"

        mock_embedder = MagicMock()
        mock_embedder.encode.return_value = [[0.1] * 3]

        mock_checkpoint = MagicMock()
        mock_checkpoint._data = {"processed": {}}
        mock_checkpoint.mark_processed.return_value = None

        # Write three files into tmp_path.
        for i in range(3):
            (tmp_path / f"doc{i}.txt").write_text(f"content of document {i} " * 30)

        agent = DocumentMonitorAgent.__new__(DocumentMonitorAgent)
        agent._logical_name = "docs"
        agent.chroma = mock_chroma
        agent.router = mock_router
        agent.collection = MagicMock()
        agent._embedder = mock_embedder
        agent.chunk_size = 200
        agent.chunk_overlap = 20
        agent.checkpoint = mock_checkpoint
        agent.directory = tmp_path
        agent.poll_interval_sec = 0
        agent._last_processed_file = None
        agent._last_error = None

        with __import__("unittest.mock", fromlist=["patch"]).patch("time.sleep"):
            agent._tick_impl()

        # delete and create must each be called exactly once.
        deletes = [c for c in call_order if c.startswith("delete:docs__b")]
        creates = [c for c in call_order if c.startswith("create:docs__b")]
        assert len(deletes) == 1, f"Expected 1 delete, got: {deletes}"
        assert len(creates) == 1, f"Expected 1 create, got: {creates}"

        # delete must precede create.
        assert call_order.index("delete:docs__b") < call_order.index("create:docs__b")

        # commit_swap must be called exactly once.
        mock_router.commit_swap.assert_called_once_with("docs", mock_chroma)

        # add_documents must have been called once per file (3 files).
        assert mock_chroma.add_documents.call_count == 3

        # Every add_documents call must have used the same idle_col object.
        for call in mock_chroma.add_documents.call_args_list:
            assert call[0][0] is idle_col, "add_documents was not given the idle collection"
