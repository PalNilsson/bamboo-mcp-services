"""Tests for document_monitor_agent CLI — multi-watch support.

Covers:
  - build_parser(): --watch, legacy --dir/--collection, shared flags
  - _resolve_watches(): normal, legacy, missing, both combined
  - _checkpoint_path(): naming convention
  - _build_agents(): one agent per watch pair, shared embedder
  - main() integration: --watch, --once, deprecation warning for --dir
"""

from __future__ import annotations

import warnings
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bamboo_mcp_services.agents.document_monitor_agent.cli import (
    _checkpoint_path,
    _resolve_watches,
    build_parser,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_EMBEDDER = (
    "bamboo_mcp_services.agents.document_monitor_agent.cli"
    ".LangchainHuggingFaceAdapter"
)
_AGENT_CLS = (
    "bamboo_mcp_services.agents.document_monitor_agent.cli"
    ".DocumentMonitorAgent"
)


# ===========================================================================
# build_parser — argument definitions
# ===========================================================================

class TestBuildParser:
    def test_watch_not_required_if_legacy_dir_given(self):
        """--dir alone should parse without error (legacy compat)."""
        args = build_parser().parse_args(["--dir", "/some/path"])
        assert args.legacy_dir == "/some/path"

    def test_watch_single_pair(self):
        args = build_parser().parse_args(["--watch", "/rag/panda_docs", "panda_docs"])
        assert args.watches == [["/rag/panda_docs", "panda_docs"]]

    def test_watch_multiple_pairs(self):
        args = build_parser().parse_args([
            "--watch", "/rag/panda_docs", "panda_docs",
            "--watch", "/rag/bamboo_docs", "bamboo_docs",
        ])
        assert len(args.watches) == 2
        assert args.watches[0] == ["/rag/panda_docs", "panda_docs"]
        assert args.watches[1] == ["/rag/bamboo_docs", "bamboo_docs"]

    def test_watch_requires_two_args(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["--watch", "/only_one"])

    def test_once_flag_default_false(self):
        args = build_parser().parse_args(["--watch", "/d", "col"])
        assert args.once is False

    def test_once_flag_set(self):
        args = build_parser().parse_args(["--watch", "/d", "col", "--once"])
        assert args.once is True

    def test_chroma_dir_default(self):
        args = build_parser().parse_args(["--watch", "/d", "col"])
        assert args.chroma_dir == ".chromadb"

    def test_chroma_dir_override(self):
        args = build_parser().parse_args(["--watch", "/d", "col", "--chroma-dir", "/my/chroma"])
        assert args.chroma_dir == "/my/chroma"

    def test_checkpoint_dir_default(self):
        args = build_parser().parse_args(["--watch", "/d", "col"])
        assert args.checkpoint_dir == ".document_monitor"

    def test_legacy_collection_default(self):
        args = build_parser().parse_args(["--dir", "/d"])
        assert args.legacy_collection == "atlas_docs"

    def test_legacy_collection_override(self):
        args = build_parser().parse_args(["--dir", "/d", "--collection", "panda_docs"])
        assert args.legacy_collection == "panda_docs"


# ===========================================================================
# _resolve_watches
# ===========================================================================

class TestResolveWatches:
    def _args(self, watches=None, legacy_dir=None, legacy_collection="atlas_docs"):
        ns = build_parser().parse_args([])
        ns.watches = watches
        ns.legacy_dir = legacy_dir
        ns.legacy_collection = legacy_collection
        return ns

    def test_watch_pairs_returned_as_tuples(self):
        args = self._args(watches=[["/rag/panda_docs", "panda_docs"]])
        result = _resolve_watches(args)
        assert result == [("/rag/panda_docs", "panda_docs")]

    def test_multiple_watch_pairs(self):
        args = self._args(watches=[
            ["/rag/panda_docs", "panda_docs"],
            ["/rag/bamboo_docs", "bamboo_docs"],
        ])
        result = _resolve_watches(args)
        assert len(result) == 2
        assert ("/rag/panda_docs", "panda_docs") in result
        assert ("/rag/bamboo_docs", "bamboo_docs") in result

    def test_legacy_dir_produces_single_pair(self):
        args = self._args(legacy_dir="/old/dir", legacy_collection="atlas_docs")
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            result = _resolve_watches(args)
        assert result == [("/old/dir", "atlas_docs")]
        assert any(issubclass(x.category, DeprecationWarning) for x in w)

    def test_legacy_dir_emits_deprecation_warning(self):
        args = self._args(legacy_dir="/old/dir")
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            _resolve_watches(args)
        assert len(w) == 1
        assert issubclass(w[0].category, DeprecationWarning)
        assert "--dir" in str(w[0].message)

    def test_watch_and_legacy_dir_combined(self):
        """--watch and --dir together should both be included."""
        args = self._args(
            watches=[["/rag/panda_docs", "panda_docs"]],
            legacy_dir="/old/dir",
            legacy_collection="atlas_docs",
        )
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            result = _resolve_watches(args)
        assert ("/rag/panda_docs", "panda_docs") in result
        assert ("/old/dir", "atlas_docs") in result

    def test_no_watches_exits(self):
        args = self._args()  # watches=None, legacy_dir=None
        with pytest.raises(SystemExit):
            _resolve_watches(args)


# ===========================================================================
# _checkpoint_path
# ===========================================================================

class TestCheckpointPath:
    def test_filename_contains_collection(self):
        p = _checkpoint_path(".document_monitor", "/data/bamboo/rag/panda_docs", "panda_docs")
        assert "panda_docs" in Path(p).name

    def test_filename_contains_dir_tag(self):
        p = _checkpoint_path(".document_monitor", "/data/bamboo/rag/panda_docs", "panda_docs")
        assert "panda_docs" in Path(p).name

    def test_filename_is_json(self):
        p = _checkpoint_path(".document_monitor", "/data/bamboo/rag/panda_docs", "panda_docs")
        assert p.endswith(".json")

    def test_different_dirs_same_collection_give_different_paths(self):
        p1 = _checkpoint_path(".dm", "/rag/panda_docs", "panda_docs")
        p2 = _checkpoint_path(".dm", "/rag/pilot3", "panda_docs")
        assert p1 != p2

    def test_different_collections_same_dir_give_different_paths(self):
        p1 = _checkpoint_path(".dm", "/rag/shared", "panda_docs")
        p2 = _checkpoint_path(".dm", "/rag/shared", "bamboo_docs")
        assert p1 != p2

    def test_checkpoint_dir_is_respected(self):
        p = _checkpoint_path("/my/checkpoints", "/rag/panda_docs", "panda_docs")
        assert p.startswith("/my/checkpoints")

    def test_spaces_in_dir_name_replaced(self):
        p = _checkpoint_path(".dm", "/rag/my docs", "panda_docs")
        assert " " not in Path(p).name


# ===========================================================================
# _build_agents — one agent per watch pair, shared embedder
# ===========================================================================

class TestBuildAgents:
    def test_one_agent_per_watch_pair(self, tmp_path):
        from bamboo_mcp_services.agents.document_monitor_agent.cli import _build_agents

        args = build_parser().parse_args([
            "--watch", str(tmp_path / "panda_docs"), "panda_docs",
            "--watch", str(tmp_path / "bamboo_docs"), "bamboo_docs",
            "--chroma-dir", str(tmp_path / "chroma"),
        ])
        mock_emb = MagicMock()

        with patch(_EMBEDDER, return_value=mock_emb), \
             patch(_AGENT_CLS) as mock_cls:
            mock_cls.return_value = MagicMock()
            _ = _build_agents(args)

        assert mock_cls.call_count == 2

    def test_agent_names_match_collections(self, tmp_path):
        from bamboo_mcp_services.agents.document_monitor_agent.cli import _build_agents

        args = build_parser().parse_args([
            "--watch", str(tmp_path / "panda_docs"), "panda_docs",
            "--watch", str(tmp_path / "bamboo_docs"), "bamboo_docs",
            "--chroma-dir", str(tmp_path / "chroma"),
        ])
        mock_emb = MagicMock()
        calls = []

        def capture(*a, **kw):
            calls.append(kw if not a else {"name": a[0] if a else kw.get("name"), **kw})
            return MagicMock()

        with patch(_EMBEDDER, return_value=mock_emb), \
             patch(_AGENT_CLS, side_effect=capture):
            _build_agents(args)

        names = [c.get("name") for c in calls]
        assert "panda_docs" in names
        assert "bamboo_docs" in names

    def test_embedder_instantiated_once(self, tmp_path):
        """A single embedder instance is shared across all agents."""
        from bamboo_mcp_services.agents.document_monitor_agent.cli import _build_agents

        args = build_parser().parse_args([
            "--watch", str(tmp_path / "a"), "col_a",
            "--watch", str(tmp_path / "b"), "col_b",
            "--watch", str(tmp_path / "c"), "col_c",
            "--chroma-dir", str(tmp_path / "chroma"),
        ])
        with patch(_EMBEDDER) as mock_emb_cls, \
             patch(_AGENT_CLS, return_value=MagicMock()):
            mock_emb_cls.return_value = MagicMock()
            _build_agents(args)

        assert mock_emb_cls.call_count == 1

    def test_distinct_checkpoint_files(self, tmp_path):
        """Each agent receives a unique checkpoint file path."""
        from bamboo_mcp_services.agents.document_monitor_agent.cli import _build_agents

        args = build_parser().parse_args([
            "--watch", str(tmp_path / "panda_docs"), "panda_docs",
            "--watch", str(tmp_path / "bamboo_docs"), "bamboo_docs",
            "--chroma-dir", str(tmp_path / "chroma"),
        ])
        cp_files = []

        def capture_cp(**kw):
            cp_files.append(kw.get("checkpoint_file"))
            return MagicMock()

        with patch(_EMBEDDER, return_value=MagicMock()), \
             patch(_AGENT_CLS, side_effect=lambda *a, **kw: capture_cp(**kw)):
            _build_agents(args)

        assert len(set(cp_files)) == 2, "checkpoint files must be distinct"


# ===========================================================================
# main() integration — smoke tests (all I/O mocked)
# ===========================================================================

class TestMain:
    """Smoke tests for main() using --watch and --once with everything mocked."""

    def _mock_agent(self):
        m = MagicMock()
        m.state.name = "RUNNING"
        return m

    def test_no_watches_exits_nonzero(self):
        """main() with neither --watch nor --dir should call sys.exit."""
        from bamboo_mcp_services.agents.document_monitor_agent.cli import main
        with pytest.raises(SystemExit):
            main([])

    def test_single_watch_once_runs_without_error(self, tmp_path):
        from bamboo_mcp_services.agents.document_monitor_agent.cli import main
        agent = self._mock_agent()
        with patch(_EMBEDDER, return_value=MagicMock()), \
             patch(_AGENT_CLS, return_value=agent):
            main([
                "--watch", str(tmp_path / "panda_docs"), "panda_docs",
                "--chroma-dir", str(tmp_path / "chroma"),
                "--once",
            ])
        agent.start.assert_called_once()
        agent.tick.assert_called_once()
        agent.stop.assert_called()

    def test_multiple_watches_each_ticked_once(self, tmp_path):
        from bamboo_mcp_services.agents.document_monitor_agent.cli import main
        agents = [self._mock_agent(), self._mock_agent()]
        idx = [0]

        def make_agent(**kw):
            a = agents[idx[0] % len(agents)]
            idx[0] += 1
            return a

        with patch(_EMBEDDER, return_value=MagicMock()), \
             patch(_AGENT_CLS, side_effect=lambda *a, **kw: make_agent(**kw)):
            main([
                "--watch", str(tmp_path / "panda_docs"), "panda_docs",
                "--watch", str(tmp_path / "bamboo_docs"), "bamboo_docs",
                "--chroma-dir", str(tmp_path / "chroma"),
                "--once",
            ])

        for a in agents:
            a.start.assert_called_once()
            a.tick.assert_called_once()

    def test_legacy_dir_still_works(self, tmp_path):
        """--dir/--collection legacy flags must still produce a running agent."""
        from bamboo_mcp_services.agents.document_monitor_agent.cli import main
        agent = self._mock_agent()
        with patch(_EMBEDDER, return_value=MagicMock()), \
             patch(_AGENT_CLS, return_value=agent), \
             warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            main([
                "--dir", str(tmp_path / "legacy"),
                "--collection", "atlas_docs",
                "--chroma-dir", str(tmp_path / "chroma"),
                "--once",
            ])
        agent.start.assert_called_once()
        agent.tick.assert_called_once()


# ===========================================================================
# build_parser — --model-path flag
# ===========================================================================

class TestModelPathFlag:
    """Tests for the --model-path CLI flag and _build_embedder behaviour."""

    def test_model_path_default_is_none(self):
        """Omitting --model-path leaves model_path as None."""
        args = build_parser().parse_args(["--watch", "/d", "col"])
        assert args.model_path is None

    def test_model_path_is_accepted(self):
        """--model-path stores the supplied path string."""
        args = build_parser().parse_args([
            "--watch", "/d", "col",
            "--model-path", "/data/models/all-MiniLM-L6-v2",
        ])
        assert args.model_path == "/data/models/all-MiniLM-L6-v2"

    def test_build_embedder_no_path_uses_default_name(self):
        """When model_path=None the adapter receives the default model name."""
        from bamboo_mcp_services.agents.document_monitor_agent.cli import _build_embedder

        with patch(_EMBEDDER) as mock_cls:
            mock_cls.return_value = MagicMock()
            _build_embedder(model_path=None)

        mock_cls.assert_called_once_with(model_name="all-MiniLM-L6-v2")

    def test_build_embedder_with_path_forwards_path(self):
        """When model_path is given the adapter receives that path as model_name."""
        from bamboo_mcp_services.agents.document_monitor_agent.cli import _build_embedder

        local_path = "/data/models/all-MiniLM-L6-v2"
        with patch(_EMBEDDER) as mock_cls:
            mock_cls.return_value = MagicMock()
            _build_embedder(model_path=local_path)

        mock_cls.assert_called_once_with(model_name=local_path)

    def test_build_embedder_raises_when_path_given_and_dummy_loaded(self):
        """If model_path is set but loading falls back to DummyEmbedder, raise."""
        from bamboo_mcp_services.agents.document_monitor_agent.cli import _build_embedder
        from bamboo_mcp_services.agents.document_monitor_agent.embedder_langchain_hf import (
            DummyEmbedder,
            LangchainHuggingFaceAdapter,
        )

        fake_adapter = MagicMock(spec=LangchainHuggingFaceAdapter)
        fake_adapter._embedder = DummyEmbedder()

        with patch(_EMBEDDER, return_value=fake_adapter):
            with pytest.raises(RuntimeError, match="--model-path"):
                _build_embedder(model_path="/nonexistent/model")

    def test_build_embedder_does_not_raise_when_no_path_and_dummy(self):
        """Without --model-path a DummyEmbedder fallback is silent (dev/CI)."""
        from bamboo_mcp_services.agents.document_monitor_agent.cli import _build_embedder
        from bamboo_mcp_services.agents.document_monitor_agent.embedder_langchain_hf import (
            DummyEmbedder,
            LangchainHuggingFaceAdapter,
        )

        fake_adapter = MagicMock(spec=LangchainHuggingFaceAdapter)
        fake_adapter._embedder = DummyEmbedder()

        with patch(_EMBEDDER, return_value=fake_adapter):
            result = _build_embedder(model_path=None)  # must not raise

        assert result is fake_adapter

    def test_build_embedder_does_not_raise_when_real_embedder_loaded(self):
        """If --model-path is set and a real model loads, no exception is raised."""
        from bamboo_mcp_services.agents.document_monitor_agent.cli import _build_embedder
        from bamboo_mcp_services.agents.document_monitor_agent.embedder_langchain_hf import (
            LangchainHuggingFaceAdapter,
        )

        # Simulate a successfully loaded real embedder (not DummyEmbedder).
        fake_real_embedder = MagicMock()
        fake_adapter = MagicMock(spec=LangchainHuggingFaceAdapter)
        fake_adapter._embedder = fake_real_embedder  # not a DummyEmbedder instance

        with patch(_EMBEDDER, return_value=fake_adapter):
            result = _build_embedder(model_path="/data/models/all-MiniLM-L6-v2")

        assert result is fake_adapter

    def test_build_agents_passes_model_path_to_embedder(self, tmp_path):
        """_build_agents must forward args.model_path to _build_embedder."""
        from bamboo_mcp_services.agents.document_monitor_agent.cli import _build_agents

        args = build_parser().parse_args([
            "--watch", str(tmp_path / "panda_docs"), "panda_docs",
            "--chroma-dir", str(tmp_path / "chroma"),
            "--model-path", "/data/models/all-MiniLM-L6-v2",
        ])

        calls = []

        def fake_hf(model_name):
            calls.append(model_name)
            return MagicMock()

        with patch(_EMBEDDER, side_effect=fake_hf), \
             patch(_AGENT_CLS, return_value=MagicMock()):
            _build_agents(args)

        assert calls == ["/data/models/all-MiniLM-L6-v2"]

    def test_main_exits_when_model_path_invalid(self, tmp_path):
        """main() must propagate RuntimeError from _build_embedder as SystemExit."""
        from bamboo_mcp_services.agents.document_monitor_agent.cli import main
        from bamboo_mcp_services.agents.document_monitor_agent.embedder_langchain_hf import (
            DummyEmbedder,
            LangchainHuggingFaceAdapter,
        )

        # Simulate a load failure: adapter wraps DummyEmbedder despite a path given.
        fake_adapter = MagicMock(spec=LangchainHuggingFaceAdapter)
        fake_adapter._embedder = DummyEmbedder()

        with patch(_EMBEDDER, return_value=fake_adapter), \
             pytest.raises((RuntimeError, SystemExit)):
            main([
                "--watch", str(tmp_path / "panda_docs"), "panda_docs",
                "--chroma-dir", str(tmp_path / "chroma"),
                "--model-path", "/nonexistent/model",
                "--once",
            ])
