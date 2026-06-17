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
  live/idle slot resolution, first-run default (no eager sidecar write),
  commit_swap routing update, sidecar atomicity, post-swap empty-slot invariant
  enforcement, and verify_routing_invariant diagnostics.
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

    def test_first_access_does_not_write_sidecar(self, tmp_path):
        """live_name must NOT write the sidecar on first access.

        The sidecar must only reflect slots that have been successfully populated.
        Writing on first access would leave the sidecar pointing at an empty
        collection if the process crashed before the first commit_swap.
        """
        sidecar = tmp_path / "routing.json"
        router = CollectionRouter(str(sidecar))
        router.live_name("docs")
        # Sidecar must not exist yet — no ingestion has occurred.
        assert not sidecar.exists(), (
            "live_name() must not write the sidecar before commit_swap completes"
        )

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
        mock_chroma.collection_count.return_value = 10
        router.commit_swap("docs", mock_chroma)

        assert router.live_name("docs") == "docs__b"
        # idle is now __a
        assert router.idle_name("docs") == "docs__a"

    def test_commit_swap_deletes_old_live(self, tmp_path):
        sidecar = str(tmp_path / "routing.json")
        router = CollectionRouter(sidecar)
        router.live_name("docs")  # initialise to __a in memory

        mock_chroma = MagicMock(spec=ChromaWrapper)
        mock_chroma.collection_count.return_value = 10
        router.commit_swap("docs", mock_chroma)

        mock_chroma.delete_collection.assert_called_once_with("docs__a")

    def test_commit_swap_alternates_on_repeated_calls(self, tmp_path):
        sidecar = str(tmp_path / "routing.json")
        router = CollectionRouter(sidecar)
        mock_chroma = MagicMock(spec=ChromaWrapper)
        mock_chroma.collection_count.return_value = 10

        expected_live = ["docs__a", "docs__b", "docs__a", "docs__b"]
        for expected in expected_live:
            assert router.live_name("docs") == expected
            router.commit_swap("docs", mock_chroma)

    def test_sidecar_persists_across_instances(self, tmp_path):
        """A new CollectionRouter instance reads the sidecar left by a previous one."""
        sidecar = str(tmp_path / "routing.json")
        router1 = CollectionRouter(sidecar)
        mock_chroma = MagicMock(spec=ChromaWrapper)
        mock_chroma.collection_count.return_value = 10
        router1.commit_swap("docs", mock_chroma)  # live → __b

        router2 = CollectionRouter(sidecar)
        assert router2.live_name("docs") == "docs__b"

    def test_sidecar_write_is_atomic(self, tmp_path):
        """The sidecar is written via os.replace on commit_swap; no .tmp must linger."""
        sidecar = tmp_path / "routing.json"
        router = CollectionRouter(str(sidecar))
        mock_chroma = MagicMock(spec=ChromaWrapper)
        mock_chroma.collection_count.return_value = 5  # non-empty — invariant passes
        router.commit_swap("docs", mock_chroma)

        assert sidecar.exists(), "sidecar must exist after commit_swap"
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
        mock_chroma.collection_count.return_value = 10

        # Swap only "alpha"; "beta" should remain at __a.
        router.live_name("alpha")
        router.live_name("beta")
        router.commit_swap("alpha", mock_chroma)

        assert router.live_name("alpha") == "alpha__b"
        assert router.live_name("beta") == "beta__a"

    # ------------------------------------------------------------------
    # Regression: concurrent-instance sidecar overwrite bug
    # ------------------------------------------------------------------

    def test_concurrent_instances_preserve_all_entries(self, tmp_path):
        """Five independent CollectionRouter instances sharing one sidecar must
        each preserve the entries written by the others.

        Regression test for the bug where _save() was a blind overwrite:
        each instance would write only its own single-entry dict, so the
        last writer always clobbered every preceding entry, leaving the
        sidecar with only one collection after a full five-collection run.

        Each instance simulates one DocumentMonitorAgent (one logical name).
        All five share the same sidecar path, as they do in production.
        """
        sidecar = str(tmp_path / "collection_routing.json")
        logical_names = [
            "panda_docs",
            "atlas_docs",
            "bamboo_docs",
            "rucio_docs",
            "root_docs",
        ]

        mock_chroma = MagicMock(spec=ChromaWrapper)
        mock_chroma.collection_count.return_value = 100

        # Each "agent" creates its own CollectionRouter, touches its own
        # logical name, and commits a swap — sequentially, as they run in
        # the monitor.
        for name in logical_names:
            router = CollectionRouter(sidecar)
            router.live_name(name)  # seed __a in memory
            router.commit_swap(name, mock_chroma)  # write __b to sidecar

        # The sidecar must contain all five entries, not just the last one.
        import json as _json
        data = _json.loads(open(sidecar).read())

        assert set(data.keys()) == set(logical_names), (
            f"Sidecar missing entries after five independent swaps.\n"
            f"Expected: {sorted(logical_names)}\n"
            f"Got:      {sorted(data.keys())}"
        )
        for name in logical_names:
            assert data[name] == f"{name}__b", (
                f"Wrong live slot for '{name}': expected '{name}__b', got '{data[name]}'"
            )

    def test_save_merges_without_clobbering_existing_entries(self, tmp_path):
        """_save() must merge self._data into the on-disk state, not replace it.

        Write two entries independently and verify neither is lost when the
        second write occurs.
        """
        sidecar = str(tmp_path / "routing.json")
        mock_chroma = MagicMock(spec=ChromaWrapper)
        mock_chroma.collection_count.return_value = 10

        # First instance writes "alpha".
        r1 = CollectionRouter(sidecar)
        r1.live_name("alpha")
        r1.commit_swap("alpha", mock_chroma)

        # Second instance writes "beta" — must not erase "alpha".
        r2 = CollectionRouter(sidecar)
        r2.live_name("beta")
        r2.commit_swap("beta", mock_chroma)

        import json as _json
        data = _json.loads(open(sidecar).read())
        assert "alpha" in data, "alpha entry was clobbered by beta's _save()"
        assert "beta" in data, "beta entry was not written"
        assert data["alpha"] == "alpha__b"
        assert data["beta"] == "beta__b"

    # ------------------------------------------------------------------
    # New tests: commit_swap invariant enforcement
    # ------------------------------------------------------------------

    def test_commit_swap_raises_when_new_slot_is_empty(self, tmp_path):
        """commit_swap must raise RuntimeError if the newly live collection is empty.

        An empty slot after ingestion means no chunks were produced — RAG
        queries would silently return nothing.  The error forces the cycle to
        fail loudly instead of silently serving an empty corpus.
        """
        sidecar = str(tmp_path / "routing.json")
        router = CollectionRouter(sidecar)
        mock_chroma = MagicMock(spec=ChromaWrapper)
        mock_chroma.collection_count.return_value = 0  # empty!

        with pytest.raises(RuntimeError, match="invariant violated"):
            router.commit_swap("docs", mock_chroma)

    def test_commit_swap_writes_sidecar_before_invariant_check(self, tmp_path):
        """The sidecar is written even when the invariant check fails.

        The sidecar is the source of truth for readers; it must be updated
        atomically.  The invariant error signals the operator, but the slot
        is genuinely live and will be repopulated on the next cycle.
        """
        sidecar_path = tmp_path / "routing.json"
        router = CollectionRouter(str(sidecar_path))
        mock_chroma = MagicMock(spec=ChromaWrapper)
        mock_chroma.collection_count.return_value = 0

        with pytest.raises(RuntimeError):
            router.commit_swap("docs", mock_chroma)

        # Sidecar must have been written (swap recorded) despite the error.
        assert sidecar_path.exists()
        data = json.loads(sidecar_path.read_text())
        assert data["docs"] == "docs__b"

    def test_commit_swap_no_raise_when_count_positive(self, tmp_path):
        """commit_swap must not raise when the new slot contains documents."""
        sidecar = str(tmp_path / "routing.json")
        router = CollectionRouter(sidecar)
        mock_chroma = MagicMock(spec=ChromaWrapper)
        mock_chroma.collection_count.return_value = 42

        # Should not raise.
        router.commit_swap("docs", mock_chroma)
        assert router.live_name("docs") == "docs__b"

    # ------------------------------------------------------------------
    # New tests: verify_routing_invariant
    # ------------------------------------------------------------------

    def test_verify_routing_invariant_passes_all_ok(self, tmp_path):
        """Returns a list of OK lines when all entries are non-empty."""
        sidecar = str(tmp_path / "routing.json")
        router = CollectionRouter(sidecar)
        mock_chroma = MagicMock(spec=ChromaWrapper)
        mock_chroma.collection_count.return_value = 10
        router.commit_swap("docs", mock_chroma)  # sidecar now has docs -> docs__b

        mock_chroma.all_collection_counts.return_value = {"docs__b": 10}
        lines = router.verify_routing_invariant(mock_chroma)

        assert len(lines) == 1
        assert lines[0].startswith("[OK]")
        assert "docs__b" in lines[0]

    def test_verify_routing_invariant_raises_on_empty_collection(self, tmp_path):
        """Raises RuntimeError when a routed collection is empty."""
        sidecar_path = tmp_path / "routing.json"
        # Manually write a sidecar that points at an empty slot.
        sidecar_path.write_text(
            '{"atlas_docs": "atlas_docs__a"}', encoding="utf-8"
        )
        router = CollectionRouter(str(sidecar_path))
        mock_chroma = MagicMock(spec=ChromaWrapper)
        mock_chroma.all_collection_counts.return_value = {"atlas_docs__a": 0}

        with pytest.raises(RuntimeError, match="invariant violated"):
            router.verify_routing_invariant(mock_chroma)

    def test_verify_routing_invariant_raises_on_missing_collection(self, tmp_path):
        """Raises RuntimeError when the routed physical collection does not exist."""
        sidecar_path = tmp_path / "routing.json"
        sidecar_path.write_text(
            '{"atlas_docs": "atlas_docs__b"}', encoding="utf-8"
        )
        router = CollectionRouter(str(sidecar_path))
        mock_chroma = MagicMock(spec=ChromaWrapper)
        # atlas_docs__b is absent from ChromaDB.
        mock_chroma.all_collection_counts.return_value = {}

        with pytest.raises(RuntimeError, match="invariant violated"):
            router.verify_routing_invariant(mock_chroma)

    def test_verify_routing_invariant_reports_all_broken_entries(self, tmp_path):
        """All broken entries are included in the RuntimeError message."""
        sidecar_path = tmp_path / "routing.json"
        sidecar_path.write_text(
            '{"atlas_docs": "atlas_docs__a", "bamboo_docs": "bamboo_docs__b"}',
            encoding="utf-8",
        )
        router = CollectionRouter(str(sidecar_path))
        mock_chroma = MagicMock(spec=ChromaWrapper)
        # Both collections are empty.
        mock_chroma.all_collection_counts.return_value = {
            "atlas_docs__a": 0,
            "bamboo_docs__b": 0,
        }

        with pytest.raises(RuntimeError) as exc_info:
            router.verify_routing_invariant(mock_chroma)

        msg = str(exc_info.value)
        assert "atlas_docs" in msg
        assert "bamboo_docs" in msg

    def test_verify_routing_invariant_mixed_ok_and_broken(self, tmp_path):
        """Only the broken entries appear in the error; OK entries are not mentioned."""
        sidecar_path = tmp_path / "routing.json"
        sidecar_path.write_text(
            '{"good_docs": "good_docs__a", "bad_docs": "bad_docs__b"}',
            encoding="utf-8",
        )
        router = CollectionRouter(str(sidecar_path))
        mock_chroma = MagicMock(spec=ChromaWrapper)
        mock_chroma.all_collection_counts.return_value = {
            "good_docs__a": 50,   # OK
            "bad_docs__b": 0,     # BROKEN
        }

        with pytest.raises(RuntimeError) as exc_info:
            router.verify_routing_invariant(mock_chroma)

        msg = str(exc_info.value)
        assert "bad_docs" in msg
        # good_docs is fine — must not appear in the error
        assert "good_docs" not in msg

    def test_verify_routing_invariant_empty_sidecar_passes(self, tmp_path):
        """An empty sidecar (no entries) passes the invariant trivially."""
        sidecar = str(tmp_path / "routing.json")
        router = CollectionRouter(sidecar)  # no commits yet; sidecar absent
        mock_chroma = MagicMock(spec=ChromaWrapper)
        mock_chroma.all_collection_counts.return_value = {}

        lines = router.verify_routing_invariant(mock_chroma)
        assert lines == []


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


# ===========================================================================
# Regression: sidecar slot overwrite — stale _data clobbers a peer's swap
# ===========================================================================


class TestCollectionRouterSidecarSlotOverwrite:
    """Regression tests for the bug where a late-swapping agent overwrote a
    previously-swapped agent's sidecar entry with a stale value.

    Root cause: ``_load()`` populated ``self._data`` with the **entire**
    on-disk sidecar.  When a second agent committed its swap, ``_save()``
    called ``merged.update(self._data)``, which overlaid stale entries for
    *all other* collections on top of whatever was currently on disk — erasing
    any fresh values written by agents that swapped earlier in the same cycle.

    Fix: ``self._data`` is now scoped to only the entries this instance owns.
    ``_load()`` is a no-op; ``live_name()`` does a lazy per-key disk lookup.
    ``_save()`` does not sync ``self._data`` back from the merged result.
    """

    def test_second_swap_does_not_clobber_first_swap(self, tmp_path):
        """If two agents swap in sequence, both sidecar entries must reflect
        their *new* slots — the second swap must not revert the first one.

        This is the exact production symptom from the 11:03 monitor run:
        bamboo_docs swapped __b → __a (written to sidecar); a subsequent
        agent then wrote its own swap and its stale ``bamboo_docs__b`` value
        overwrote the freshly written ``bamboo_docs__a``.
        """
        sidecar = str(tmp_path / "routing.json")

        # Seed: both collections are at __b (result of previous run).
        initial = {"alpha": "alpha__b", "beta": "beta__b"}
        (tmp_path / "routing.json").write_text(
            json.dumps(initial, indent=2), encoding="utf-8"
        )

        mock_chroma = MagicMock(spec=ChromaWrapper)
        mock_chroma.collection_count.return_value = 50

        # Both routers load their state at (approximately) the same time,
        # *before* either has swapped.  This is the critical window: each
        # router's stale view of the other's collection must not be written
        # back to disk.
        router_alpha = CollectionRouter(sidecar)
        router_beta = CollectionRouter(sidecar)
        router_alpha.live_name("alpha")  # sees alpha__b
        router_beta.live_name("beta")    # sees beta__b

        # alpha swaps first: __b → __a
        router_alpha.commit_swap("alpha", mock_chroma)

        data = json.loads((tmp_path / "routing.json").read_text())
        assert data["alpha"] == "alpha__a", "alpha should be at __a after swap"
        assert data["beta"] == "beta__b", "beta unchanged after alpha-only swap"

        # beta swaps second: __b → __a.  Must NOT revert alpha back to __b.
        router_beta.commit_swap("beta", mock_chroma)

        data = json.loads((tmp_path / "routing.json").read_text())
        assert data["alpha"] == "alpha__a", (
            "alpha was reverted to __b by beta's commit_swap — sidecar clobber bug"
        )
        assert data["beta"] == "beta__a", "beta should be at __a after its own swap"

    def test_five_agents_swap_in_alternating_cycle(self, tmp_path):
        """Full five-collection scenario matching production topology.

        All five routers are created and call live_name() before any swap
        occurs (simulating the sequential-but-overlapping startup window).
        Then each commits its swap.  Every collection must end up at its
        new slot regardless of swap order.
        """
        sidecar = str(tmp_path / "routing.json")
        names = ["panda_docs", "atlas_docs", "bamboo_docs", "rucio_docs", "root_docs"]

        # Previous cycle left everything at __b.
        (tmp_path / "routing.json").write_text(
            json.dumps({n: f"{n}__b" for n in names}, indent=2), encoding="utf-8"
        )

        mock_chroma = MagicMock(spec=ChromaWrapper)
        mock_chroma.collection_count.return_value = 80

        # All five routers load before any swap.
        routers = {}
        for name in names:
            r = CollectionRouter(sidecar)
            r.live_name(name)
            routers[name] = r

        # Each commits a swap sequentially.
        for name in names:
            routers[name].commit_swap(name, mock_chroma)

        data = json.loads((tmp_path / "routing.json").read_text())
        assert set(data.keys()) == set(names), "All five entries must be present"
        for name in names:
            assert data[name] == f"{name}__a", (
                f"{name} should be at __a after swap, got {data[name]!r}. "
                "A stale-_data clobber from a subsequent agent's commit_swap is "
                "the likely cause."
            )

    def test_non_swapping_agents_do_not_alter_sidecar(self, tmp_path):
        """Agents that find no new files must not write to the sidecar at all.

        In a cycle where only one collection has changed files, the other four
        collections must not alter the sidecar — not even to re-write the same
        value.  This test ensures that simply calling live_name() (which all
        agents do at startup) does not trigger a sidecar write.
        """
        sidecar_path = tmp_path / "routing.json"
        names = ["panda_docs", "atlas_docs", "bamboo_docs", "rucio_docs", "root_docs"]

        (sidecar_path).write_text(
            json.dumps({n: f"{n}__b" for n in names}, indent=2), encoding="utf-8"
        )
        mtime_before = sidecar_path.stat().st_mtime

        # Simulate agents that do NOT swap (no new files) — they only call live_name().
        for name in names:
            r = CollectionRouter(str(sidecar_path))
            r.live_name(name)
            # No commit_swap call.

        mtime_after = sidecar_path.stat().st_mtime
        assert mtime_before == mtime_after, (
            "Sidecar was modified by a live_name()-only path; only commit_swap() "
            "should write to the sidecar."
        )
