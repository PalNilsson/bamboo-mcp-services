"""Unit tests for document_monitor_agent CLI helpers.

Covers:
- :func:`~bamboo_mcp_services.agents.document_monitor_agent.cli.build_parser`:
  default values, ``--log-file`` argument present and stored.
- :func:`~bamboo_mcp_services.agents.document_monitor_agent.cli._configure_logging`:
  no-op when *log_file* is ``None``; file handler added when *log_file* is set;
  parent directories created automatically; correct formatter; ``_SuppressNameAtInfo``
  attached to the file handler.
- :func:`~bamboo_mcp_services.agents.document_monitor_agent.cli._resolve_watches`:
  ``--watch`` pairs, legacy ``--dir``/``--collection`` deprecation, missing
  watch exits.
- :func:`~bamboo_mcp_services.agents.document_monitor_agent.cli._checkpoint_path`:
  deterministic filename derivation.
- :class:`~bamboo_mcp_services.agents.document_monitor_agent.cli._SuppressNameAtInfo`:
  blanks name at INFO, preserves it at WARNING+.
"""

from __future__ import annotations

import logging
import warnings
from logging.handlers import RotatingFileHandler
from types import SimpleNamespace

import pytest

from bamboo_mcp_services.agents.document_monitor_agent.cli import (
    _SuppressNameAtInfo,
    _checkpoint_path,
    _configure_logging,
    _resolve_watches,
    build_parser,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s%(message)s"


def _make_args(**kwargs) -> SimpleNamespace:
    """Return a minimal argparse-like namespace for _resolve_watches."""
    defaults = dict(watches=None, legacy_dir=None, legacy_collection="atlas_docs")
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


# ===========================================================================
# build_parser
# ===========================================================================


class TestBuildParser:
    """Smoke-test the argument parser defaults and --log-file registration."""

    def test_log_file_default_is_none(self):
        parser = build_parser()
        args = parser.parse_args(["--watch", "/data/docs", "atlas_docs"])
        assert args.log_file is None

    def test_log_file_is_stored(self, tmp_path):
        parser = build_parser()
        log_path = str(tmp_path / "monitor.log")
        args = parser.parse_args([
            "--watch", "/data/docs", "atlas_docs",
            "--log-file", log_path,
        ])
        assert args.log_file == log_path

    def test_once_default_is_false(self):
        parser = build_parser()
        args = parser.parse_args(["--watch", "/data/docs", "atlas_docs"])
        assert args.once is False

    def test_poll_interval_default(self):
        parser = build_parser()
        args = parser.parse_args(["--watch", "/data/docs", "atlas_docs"])
        assert args.poll_interval == 10

    def test_chroma_dir_default(self):
        parser = build_parser()
        args = parser.parse_args(["--watch", "/data/docs", "atlas_docs"])
        assert args.chroma_dir == ".chromadb"

    def test_model_path_default_is_none(self):
        parser = build_parser()
        args = parser.parse_args(["--watch", "/data/docs", "atlas_docs"])
        assert args.model_path is None


# ===========================================================================
# _SuppressNameAtInfo
# ===========================================================================


class TestSuppressNameAtInfo:
    """The filter must blank name at INFO and leave it intact at WARNING+."""

    def _record(self, level: int, name: str = "some.logger") -> logging.LogRecord:
        r = logging.LogRecord(
            name=name, level=level, pathname="", lineno=0,
            msg="msg", args=(), exc_info=None,
        )
        return r

    def test_blanks_name_at_info(self):
        f = _SuppressNameAtInfo()
        record = self._record(logging.INFO)
        f.filter(record)
        assert record.name == ""

    def test_preserves_name_at_warning(self):
        f = _SuppressNameAtInfo()
        record = self._record(logging.WARNING)
        f.filter(record)
        assert record.name == "some.logger"

    def test_preserves_name_at_error(self):
        f = _SuppressNameAtInfo()
        record = self._record(logging.ERROR)
        f.filter(record)
        assert record.name == "some.logger"

    def test_always_returns_true(self):
        f = _SuppressNameAtInfo()
        for level in (logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR):
            assert f.filter(self._record(level)) is True


# ===========================================================================
# _configure_logging
# ===========================================================================


class TestConfigureLogging:
    """_configure_logging must be a no-op when log_file is None and must
    attach a RotatingFileHandler with the correct properties otherwise."""

    def _root_file_handlers(self) -> list[RotatingFileHandler]:
        return [
            h for h in logging.getLogger().handlers
            if isinstance(h, RotatingFileHandler)
        ]

    def _remove_file_handlers(self) -> None:
        root = logging.getLogger()
        for h in self._root_file_handlers():
            root.removeHandler(h)
            h.close()

    def setup_method(self):
        self._remove_file_handlers()

    def teardown_method(self):
        self._remove_file_handlers()

    def test_noop_when_log_file_is_none(self):
        before = len(logging.getLogger().handlers)
        _configure_logging(None, _SuppressNameAtInfo())
        assert len(logging.getLogger().handlers) == before

    def test_adds_rotating_file_handler(self, tmp_path):
        log_path = str(tmp_path / "test.log")
        _configure_logging(log_path, _SuppressNameAtInfo())
        handlers = self._root_file_handlers()
        assert len(handlers) == 1

    def test_file_is_created(self, tmp_path):
        log_path = tmp_path / "test.log"
        _configure_logging(str(log_path), _SuppressNameAtInfo())
        # Emit a record so the handler actually opens the file.
        logging.getLogger("test").info("hello")
        assert log_path.exists()

    def test_parent_dirs_created_automatically(self, tmp_path):
        nested = tmp_path / "a" / "b" / "c" / "monitor.log"
        assert not nested.parent.exists()
        _configure_logging(str(nested), _SuppressNameAtInfo())
        assert nested.parent.exists()

    def test_handler_uses_correct_format(self, tmp_path):
        log_path = str(tmp_path / "fmt.log")
        _configure_logging(log_path, _SuppressNameAtInfo())
        handlers = self._root_file_handlers()
        assert handlers[0].formatter._fmt == LOG_FORMAT

    def test_handler_rotation_config(self, tmp_path):
        log_path = str(tmp_path / "rotate.log")
        _configure_logging(log_path, _SuppressNameAtInfo())
        handler = self._root_file_handlers()[0]
        assert handler.maxBytes == 10 * 1024 * 1024
        assert handler.backupCount == 5

    def test_suppress_filter_attached_to_file_handler(self, tmp_path):
        log_path = str(tmp_path / "filtered.log")
        suppress = _SuppressNameAtInfo()
        _configure_logging(log_path, suppress)
        handler = self._root_file_handlers()[0]
        assert suppress in handler.filters

    def test_info_name_suppressed_in_file(self, tmp_path):
        """INFO records written to the file must have a blank name field."""
        log_path = tmp_path / "out.log"
        _configure_logging(str(log_path), _SuppressNameAtInfo())
        # Ensure root logger passes INFO records through (pytest sets it to WARNING).
        root = logging.getLogger()
        original_level = root.level
        root.setLevel(logging.INFO)
        try:
            logging.getLogger("mymodule").info("test message")
            for h in self._root_file_handlers():
                h.flush()
        finally:
            root.setLevel(original_level)
        content = log_path.read_text(encoding="utf-8")
        # The logger name "mymodule" must NOT appear (was blanked by filter).
        assert "mymodule" not in content
        assert "test message" in content

    def test_warning_name_preserved_in_file(self, tmp_path):
        """WARNING+ records must keep their logger name in the file."""
        log_path = tmp_path / "warn.log"
        _configure_logging(str(log_path), _SuppressNameAtInfo())
        logging.getLogger("mymodule").warning("something wrong")
        for h in self._root_file_handlers():
            h.flush()
        content = log_path.read_text(encoding="utf-8")
        assert "mymodule" in content


# ===========================================================================
# _resolve_watches
# ===========================================================================


class TestResolveWatches:
    """_resolve_watches must merge --watch and legacy --dir/--collection."""

    def test_single_watch_pair(self):
        args = _make_args(watches=[["/data/docs", "atlas_docs"]])
        result = _resolve_watches(args)
        assert result == [("/data/docs", "atlas_docs")]

    def test_multiple_watch_pairs(self):
        args = _make_args(watches=[
            ["/data/panda", "panda_docs"],
            ["/data/bamboo", "bamboo_docs"],
        ])
        result = _resolve_watches(args)
        assert ("/data/panda", "panda_docs") in result
        assert ("/data/bamboo", "bamboo_docs") in result
        assert len(result) == 2

    def test_legacy_dir_emits_deprecation_warning(self):
        args = _make_args(legacy_dir="/old/dir", legacy_collection="atlas_docs")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = _resolve_watches(args)
        assert any(issubclass(w.category, DeprecationWarning) for w in caught)
        assert result == [("/old/dir", "atlas_docs")]

    def test_legacy_merged_with_watch(self):
        args = _make_args(
            watches=[["/data/panda", "panda_docs"]],
            legacy_dir="/old/atlas",
            legacy_collection="atlas_docs",
        )
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            result = _resolve_watches(args)
        assert ("/data/panda", "panda_docs") in result
        assert ("/old/atlas", "atlas_docs") in result

    def test_no_watches_exits(self):
        args = _make_args()
        with pytest.raises(SystemExit):
            _resolve_watches(args)


# ===========================================================================
# _checkpoint_path
# ===========================================================================


class TestCheckpointPath:
    """_checkpoint_path must produce deterministic, unique filenames."""

    def test_basic_derivation(self):
        result = _checkpoint_path(".document_monitor", "/data/bamboo/rag/panda_docs", "panda_docs")
        assert result == ".document_monitor/checkpoints_panda_docs_panda_docs.json"

    def test_uses_last_path_component(self):
        result = _checkpoint_path(".ckpt", "/some/deep/path/my_dir", "my_col")
        assert "my_dir" in result
        assert "path" not in result

    def test_spaces_replaced_with_underscores(self):
        result = _checkpoint_path(".ckpt", "/data/my docs", "col")
        assert " " not in result
        assert "my_docs" in result

    def test_different_dirs_same_collection_differ(self):
        r1 = _checkpoint_path(".ckpt", "/data/dirA", "docs")
        r2 = _checkpoint_path(".ckpt", "/data/dirB", "docs")
        assert r1 != r2

    def test_same_dir_different_collections_differ(self):
        r1 = _checkpoint_path(".ckpt", "/data/docs", "col_a")
        r2 = _checkpoint_path(".ckpt", "/data/docs", "col_b")
        assert r1 != r2
