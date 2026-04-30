"""Tests for the github_doc_sync_agent package.

Covers:
- GithubDocSyncer: interval gate, run_cycle success and failure modes,
  health attribute updates
- GithubDocSyncAgent: lifecycle (start/tick/stop state transitions),
  health reporting, config validation
- CLI: argument parsing, config validation, --once end-to-end
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from bamboo_mcp_services.agents.base import AgentState
from bamboo_mcp_services.agents.github_doc_sync_agent.agent import (
    GithubDocSyncAgent,
    GithubDocSyncConfig,
)
from bamboo_mcp_services.agents.github_doc_sync_agent.cli import build_parser, main
from bamboo_mcp_services.agents.github_doc_sync_agent.github_doc_syncer import (
    GithubDocSyncer,
)
from bamboo_mcp_services.agents.github_doc_sync_agent.github_markdown_sync import (
    RepoConfig,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _repo(name: str = "owner/repo", destination: str = "/tmp/dest") -> RepoConfig:
    """Build a minimal RepoConfig for testing."""
    return RepoConfig(name=name, destination=destination)


def _syncer(*repos: RepoConfig, interval: int = 0) -> GithubDocSyncer:
    """Build a GithubDocSyncer with refresh_interval_s=0 so every tick runs."""
    return GithubDocSyncer(repos=list(repos), refresh_interval_s=interval)


#: Full path to sync_repo inside the syncer module — used as the patch target.
_SYNC_REPO = (
    "bamboo_mcp_services.agents.github_doc_sync_agent"
    ".github_doc_syncer.sync_repo"
)


# ===========================================================================
# GithubDocSyncer — interval gate
# ===========================================================================

class TestSyncerIntervalGate:
    def test_first_call_runs_when_interval_zero(self):
        syncer = _syncer(_repo(), interval=0)
        with patch(_SYNC_REPO):
            result = syncer.run_cycle()
        assert result is True

    def test_second_immediate_call_is_skipped(self):
        syncer = _syncer(_repo(), interval=9999)
        # Pre-arm the gate so the interval appears to have just been recorded.
        syncer._last_attempt = time.monotonic()
        with patch(_SYNC_REPO) as mock_sync:
            result = syncer.run_cycle()  # interval not elapsed
        assert result is False
        assert mock_sync.call_count == 0  # gate blocked, never called

    def test_call_after_interval_elapsed_runs(self):
        syncer = _syncer(_repo(), interval=0)
        with patch(_SYNC_REPO):
            syncer.run_cycle()
            # Force interval to have elapsed by winding _last_attempt back.
            syncer._last_attempt = 0.0
            result = syncer.run_cycle()
        assert result is True

    def test_skipped_cycle_leaves_health_attrs_at_none(self):
        syncer = _syncer(_repo(), interval=9999)
        # Pretend a previous run already set _last_attempt.
        syncer._last_attempt = time.monotonic()
        syncer.run_cycle()
        assert syncer.last_sync_utc is None

    def test_interval_gate_uses_monotonic_not_wall_clock(self):
        """The gate must survive machines that have been running for days."""
        syncer = _syncer(_repo(), interval=0)
        syncer._last_attempt = 0.0  # epoch — any machine is past this
        with patch(_SYNC_REPO):
            result = syncer.run_cycle()
        assert result is True


# ===========================================================================
# GithubDocSyncer — run_cycle with no repos
# ===========================================================================

class TestSyncerNoRepos:
    def test_empty_repo_list_returns_true(self):
        syncer = GithubDocSyncer(repos=[], refresh_interval_s=0)
        result = syncer.run_cycle()
        assert result is True

    def test_empty_repo_list_updates_last_sync_utc(self):
        syncer = GithubDocSyncer(repos=[], refresh_interval_s=0)
        syncer.run_cycle()
        assert syncer.last_sync_utc is not None

    def test_empty_repo_list_sets_repos_synced_to_zero(self):
        syncer = GithubDocSyncer(repos=[], refresh_interval_s=0)
        syncer.run_cycle()
        assert syncer.last_repos_synced == 0

    def test_empty_repo_list_no_error(self):
        syncer = GithubDocSyncer(repos=[], refresh_interval_s=0)
        syncer.run_cycle()
        assert syncer.last_error_repo is None
        assert syncer.last_error_msg is None


# ===========================================================================
# GithubDocSyncer — run_cycle success
# ===========================================================================

class TestSyncerSuccess:
    def test_sync_repo_called_once_per_configured_repo(self):
        r1 = _repo("owner/repo1", "/tmp/r1")
        r2 = _repo("owner/repo2", "/tmp/r2")
        syncer = _syncer(r1, r2)
        with patch(_SYNC_REPO) as mock_sync:
            syncer.run_cycle()
        assert mock_sync.call_count == 2

    def test_sync_repo_called_with_correct_config(self):
        r = _repo("owner/myrepo", "/tmp/myrepo")
        syncer = _syncer(r)
        with patch(_SYNC_REPO) as mock_sync:
            syncer.run_cycle()
        mock_sync.assert_called_once_with(r)

    def test_repos_synced_count_updated(self):
        syncer = _syncer(_repo("a/b"), _repo("c/d"))
        with patch(_SYNC_REPO):
            syncer.run_cycle()
        assert syncer.last_repos_synced == 2

    def test_last_sync_utc_updated_after_cycle(self):
        syncer = _syncer(_repo())
        before = datetime.now(timezone.utc)
        with patch(_SYNC_REPO):
            syncer.run_cycle()
        after = datetime.now(timezone.utc)
        assert syncer.last_sync_utc is not None
        assert before <= syncer.last_sync_utc <= after

    def test_last_error_repo_cleared_after_clean_cycle(self):
        syncer = _syncer(_repo())
        # Simulate a previous error.
        syncer.last_error_repo = "owner/old-broken"
        syncer.last_error_msg = "some error"
        with patch(_SYNC_REPO):
            syncer.run_cycle()
        assert syncer.last_error_repo is None
        assert syncer.last_error_msg is None

    def test_returns_true_on_success(self):
        syncer = _syncer(_repo())
        with patch(_SYNC_REPO):
            result = syncer.run_cycle()
        assert result is True


# ===========================================================================
# GithubDocSyncer — run_cycle with failures
# ===========================================================================

class TestSyncerFailures:
    def test_one_failing_repo_does_not_abort_others(self):
        r1 = _repo("owner/bad", "/tmp/bad")
        r2 = _repo("owner/good", "/tmp/good")
        syncer = _syncer(r1, r2)

        call_log = []

        def fake_sync(cfg):
            call_log.append(cfg.name)
            if cfg.name == "owner/bad":
                raise RuntimeError("API down")

        with patch(_SYNC_REPO, side_effect=fake_sync):
            syncer.run_cycle()

        assert "owner/bad" in call_log
        assert "owner/good" in call_log

    def test_last_error_repo_set_to_failing_repo(self):
        syncer = _syncer(_repo("owner/broken"))
        with patch(_SYNC_REPO, side_effect=RuntimeError("boom")):
            syncer.run_cycle()
        assert syncer.last_error_repo == "owner/broken"

    def test_last_error_msg_contains_exception_text(self):
        syncer = _syncer(_repo("owner/broken"))
        with patch(_SYNC_REPO, side_effect=RuntimeError("network timeout")):
            syncer.run_cycle()
        assert "network timeout" in syncer.last_error_msg

    def test_last_error_msg_includes_exception_type(self):
        syncer = _syncer(_repo("owner/broken"))
        with patch(_SYNC_REPO, side_effect=ValueError("bad config")):
            syncer.run_cycle()
        assert "ValueError" in syncer.last_error_msg

    def test_repos_synced_count_includes_failing_repos(self):
        """All repos are *attempted*; count reflects all attempts."""
        r1 = _repo("owner/a")
        r2 = _repo("owner/b")
        syncer = _syncer(r1, r2)
        with patch(_SYNC_REPO, side_effect=RuntimeError("boom")):
            syncer.run_cycle()
        assert syncer.last_repos_synced == 2

    def test_all_repos_fail_still_returns_true(self):
        syncer = _syncer(_repo("owner/a"), _repo("owner/b"))
        with patch(_SYNC_REPO, side_effect=RuntimeError("down")):
            result = syncer.run_cycle()
        assert result is True

    def test_last_error_repo_is_last_failing_not_first(self):
        """When multiple repos fail, last_error_repo records the last one."""
        r1 = _repo("owner/first")
        r2 = _repo("owner/second")
        syncer = _syncer(r1, r2)
        with patch(_SYNC_REPO, side_effect=RuntimeError("boom")):
            syncer.run_cycle()
        assert syncer.last_error_repo == "owner/second"

    def test_no_exception_propagates_from_run_cycle(self):
        """run_cycle must never raise regardless of how bad the repos are."""
        syncer = _syncer(_repo("owner/a"), _repo("owner/b"), _repo("owner/c"))
        with patch(_SYNC_REPO, side_effect=Exception("unexpected")):
            syncer.run_cycle()  # must not raise


# ===========================================================================
# GithubDocSyncer — health attributes before first run
# ===========================================================================

class TestSyncerInitialState:
    def test_last_sync_utc_none_before_first_run(self):
        syncer = _syncer(_repo())
        assert syncer.last_sync_utc is None

    def test_last_repos_synced_zero_before_first_run(self):
        syncer = _syncer(_repo())
        assert syncer.last_repos_synced == 0

    def test_last_error_repo_none_before_first_run(self):
        syncer = _syncer(_repo())
        assert syncer.last_error_repo is None

    def test_last_error_msg_none_before_first_run(self):
        syncer = _syncer(_repo())
        assert syncer.last_error_msg is None


# ===========================================================================
# GithubDocSyncAgent — lifecycle
# ===========================================================================

class TestGithubDocSyncAgent:
    def _make_config(self, repos=None, interval=0) -> GithubDocSyncConfig:
        return GithubDocSyncConfig(
            repos=repos or [_repo()],
            refresh_interval_s=interval,
        )

    def test_config_none_raises_value_error(self):
        with pytest.raises(ValueError):
            GithubDocSyncAgent(config=None)

    def test_start_transitions_to_running(self):
        agent = GithubDocSyncAgent(config=self._make_config())
        assert agent.state == AgentState.NEW
        agent.start()
        assert agent.state == AgentState.RUNNING
        agent.stop()

    def test_stop_transitions_to_stopped(self):
        agent = GithubDocSyncAgent(config=self._make_config())
        agent.start()
        agent.stop()
        assert agent.state == AgentState.STOPPED

    def test_tick_delegates_to_syncer(self):
        agent = GithubDocSyncAgent(config=self._make_config())
        with patch(_SYNC_REPO):
            agent.start()
            agent.tick()
        assert agent._syncer.last_repos_synced == 1

    def test_tick_before_start_raises_runtime_error(self):
        agent = GithubDocSyncAgent(config=self._make_config())
        with pytest.raises(RuntimeError):
            agent.tick()

    def test_stop_clears_syncer_reference(self):
        agent = GithubDocSyncAgent(config=self._make_config())
        agent.start()
        agent.stop()
        assert agent._syncer is None

    def test_start_is_idempotent(self):
        agent = GithubDocSyncAgent(config=self._make_config())
        agent.start()
        agent.start()  # second call is a no-op
        assert agent.state == AgentState.RUNNING
        agent.stop()

    def test_stop_is_idempotent(self):
        agent = GithubDocSyncAgent(config=self._make_config())
        agent.start()
        agent.stop()
        agent.stop()  # second call is a no-op
        assert agent.state == AgentState.STOPPED

    def test_syncer_receives_correct_repo_count(self):
        repos = [_repo("owner/a"), _repo("owner/b"), _repo("owner/c")]
        agent = GithubDocSyncAgent(config=self._make_config(repos=repos))
        agent.start()
        assert len(agent._syncer.repos) == 3
        agent.stop()

    def test_syncer_receives_correct_refresh_interval(self):
        agent = GithubDocSyncAgent(
            config=self._make_config(interval=1800)
        )
        agent.start()
        assert agent._syncer.refresh_interval_s == 1800
        agent.stop()


# ===========================================================================
# GithubDocSyncAgent — health reporting
# ===========================================================================

class TestGithubDocSyncAgentHealth:
    def _make_agent(self, repos=None, interval=0) -> GithubDocSyncAgent:
        cfg = GithubDocSyncConfig(
            repos=repos or [_repo("owner/repo")],
            refresh_interval_s=interval,
        )
        return GithubDocSyncAgent(config=cfg)

    def test_health_ok_while_running(self):
        agent = self._make_agent()
        agent.start()
        assert agent.health().ok is True
        agent.stop()

    def test_health_ok_after_stopped(self):
        agent = self._make_agent()
        agent.start()
        agent.stop()
        assert agent.health().ok is True

    def test_health_details_before_first_tick(self):
        agent = self._make_agent(repos=[_repo("owner/x")])
        agent.start()
        details = agent.health().details
        assert details["repo_count"] == 1
        assert details["repo_names"] == ["owner/x"]
        assert details["last_sync_utc"] is None
        assert details["last_repos_synced"] == 0
        assert details["last_error_repo"] is None
        agent.stop()

    def test_health_details_after_successful_tick(self):
        agent = self._make_agent()
        with patch(_SYNC_REPO):
            agent.start()
            agent.tick()
        details = agent.health().details
        assert details["last_sync_utc"] is not None
        assert details["last_repos_synced"] == 1
        assert details["last_error_repo"] is None
        agent.stop()

    def test_health_details_after_failing_tick(self):
        agent = self._make_agent(repos=[_repo("owner/broken")])
        with patch(_SYNC_REPO, side_effect=RuntimeError("boom")):
            agent.start()
            agent.tick()
        details = agent.health().details
        assert details["last_error_repo"] == "owner/broken"
        assert details["last_error_msg"] is not None
        agent.stop()

    def test_health_details_repo_names_matches_config(self):
        repos = [_repo("atlas/panda-docs"), _repo("atlas/harvester")]
        agent = self._make_agent(repos=repos)
        agent.start()
        details = agent.health().details
        assert details["repo_names"] == ["atlas/panda-docs", "atlas/harvester"]
        agent.stop()

    def test_health_details_refresh_interval_s_matches_config(self):
        agent = self._make_agent(interval=7200)
        agent.start()
        assert agent.health().details["refresh_interval_s"] == 7200
        agent.stop()

    def test_health_details_after_stop_syncer_gone(self):
        """After stop(), syncer is None — health should still return sane data."""
        agent = self._make_agent()
        agent.start()
        agent.stop()
        details = agent.health().details
        # repo_count and repo_names come from config, not syncer
        assert details["repo_count"] == 1
        assert details["last_sync_utc"] is None


# ===========================================================================
# CLI — argument parsing
# ===========================================================================

class TestCLIParser:
    def test_default_config_path(self):
        args = build_parser().parse_args([])
        assert "github-doc-sync-agent.yaml" in args.config

    def test_config_flag_overrides_default(self):
        args = build_parser().parse_args(["--config", "/my/repos.yaml"])
        assert args.config == "/my/repos.yaml"

    def test_once_flag_defaults_false(self):
        args = build_parser().parse_args([])
        assert args.once is False

    def test_once_flag_set(self):
        args = build_parser().parse_args(["--once"])
        assert args.once is True

    def test_log_level_default_is_info(self):
        args = build_parser().parse_args([])
        assert args.log_level == "INFO"

    def test_log_level_debug(self):
        args = build_parser().parse_args(["--log-level", "DEBUG"])
        assert args.log_level == "DEBUG"

    def test_help_exits_zero(self):
        with pytest.raises(SystemExit) as exc_info:
            build_parser().parse_args(["--help"])
        assert exc_info.value.code == 0


# ===========================================================================
# CLI — main() integration (all I/O mocked)
# ===========================================================================

class TestCLIMain:
    def _write_config(self, tmp_path: Path, content: dict) -> Path:
        cfg_path = tmp_path / "repos.yaml"
        cfg_path.write_text(yaml.dump(content))
        return cfg_path

    def test_missing_config_file_returns_1(self, tmp_path):
        result = main(["--config", str(tmp_path / "nonexistent.yaml"), "--once"])
        assert result == 1

    def test_empty_config_file_returns_0_with_warning(self, tmp_path):
        """An empty repos list is valid; agent runs but syncs nothing."""
        cfg = self._write_config(tmp_path, {"repos": []})
        with patch(_SYNC_REPO):
            result = main(["--config", str(cfg), "--once"])
        assert result == 0

    def test_repo_missing_name_returns_1(self, tmp_path):
        cfg = self._write_config(
            tmp_path, {"repos": [{"destination": "/tmp/dest"}]}
        )
        result = main(["--config", str(cfg), "--once"])
        assert result == 1

    def test_repo_missing_destination_returns_1(self, tmp_path):
        cfg = self._write_config(
            tmp_path, {"repos": [{"name": "owner/repo"}]}
        )
        result = main(["--config", str(cfg), "--once"])
        assert result == 1

    def test_once_flag_calls_sync_repo_and_returns_0(self, tmp_path):
        cfg = self._write_config(
            tmp_path,
            {
                "refresh_interval_s": 0,
                "repos": [
                    {
                        "name": "owner/repo",
                        "destination": str(tmp_path / "raw"),
                        "normalized_destination": str(tmp_path / "norm"),
                        "normalize_for_rag": True,
                    }
                ],
            },
        )
        with patch(_SYNC_REPO) as mock_sync:
            result = main(["--config", str(cfg), "--once"])
        assert result == 0
        assert mock_sync.call_count == 1
        called_cfg = mock_sync.call_args[0][0]
        assert called_cfg.name == "owner/repo"

    def test_once_with_multiple_repos_calls_sync_for_each(self, tmp_path):
        cfg = self._write_config(
            tmp_path,
            {
                "refresh_interval_s": 0,
                "repos": [
                    {"name": "owner/a", "destination": str(tmp_path / "a")},
                    {"name": "owner/b", "destination": str(tmp_path / "b")},
                ],
            },
        )
        with patch(_SYNC_REPO) as mock_sync:
            result = main(["--config", str(cfg), "--once"])
        assert result == 0
        assert mock_sync.call_count == 2

    def test_once_sync_failure_still_returns_0(self, tmp_path):
        """A repo sync error is logged but the process exits cleanly."""
        cfg = self._write_config(
            tmp_path,
            {"repos": [{"name": "owner/broken", "destination": str(tmp_path / "raw")}]},
        )
        with patch(_SYNC_REPO, side_effect=RuntimeError("API down")):
            result = main(["--config", str(cfg), "--once"])
        assert result == 0

    def test_github_token_env_var_is_read(self, tmp_path, monkeypatch):
        """GITHUB_TOKEN in environment should not cause a crash."""
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake_token")
        cfg = self._write_config(tmp_path, {"repos": []})
        with patch(_SYNC_REPO):
            result = main(["--config", str(cfg), "--once"])
        assert result == 0

    def test_refresh_interval_from_config(self, tmp_path):
        cfg = self._write_config(
            tmp_path,
            {
                "refresh_interval_s": 7200,
                "repos": [{"name": "owner/r", "destination": str(tmp_path / "r")}],
            },
        )
        captured = {}

        def fake_sync(repo_cfg):
            pass

        with patch(_SYNC_REPO, side_effect=fake_sync):
            with patch(
                "bamboo_mcp_services.agents.github_doc_sync_agent.agent.GithubDocSyncAgent.start"
            ), patch(
                "bamboo_mcp_services.agents.github_doc_sync_agent.agent.GithubDocSyncAgent.tick"
            ), patch(
                "bamboo_mcp_services.agents.github_doc_sync_agent.agent.GithubDocSyncAgent.stop"
            ):
                # Intercept the config passed to the agent.
                original_init = GithubDocSyncAgent.__init__

                def capturing_init(self_agent, name="github-doc-sync-agent", config=None):
                    captured["refresh_interval_s"] = config.refresh_interval_s
                    original_init(self_agent, name=name, config=config)

                with patch.object(GithubDocSyncAgent, "__init__", capturing_init):
                    main(["--config", str(cfg), "--once"])

        assert captured.get("refresh_interval_s") == 7200

    def test_log_file_dev_null_does_not_crash(self, tmp_path):
        cfg = self._write_config(tmp_path, {"repos": []})
        with patch(_SYNC_REPO):
            result = main(
                ["--config", str(cfg), "--once", "--log-file", "/dev/null"]
            )
        assert result == 0


# ===========================================================================
# github_markdown_sync — within_hours first-run behaviour
# ===========================================================================

class TestWithinHoursFirstRun:
    """within_hours must be ignored on a first run (no prior sync state)."""

    _GET_LATEST = (
        "bamboo_mcp_services.agents.github_doc_sync_agent"
        ".github_markdown_sync.get_latest_commit"
    )
    _GET_TREE = (
        "bamboo_mcp_services.agents.github_doc_sync_agent"
        ".github_markdown_sync._get_tree"
    )
    _DOWNLOAD = (
        "bamboo_mcp_services.agents.github_doc_sync_agent"
        ".github_markdown_sync._download_file"
    )

    def _old_commit(self):
        """Return a (sha, datetime) tuple for a commit that is 200 hours old."""
        from datetime import timedelta
        sha = "abc123def456" * 3
        dt = datetime.now(tz=timezone.utc) - timedelta(hours=200)
        return sha, dt

    def test_first_run_downloads_despite_old_commit(self, tmp_path):
        """No state file + within_hours exceeded → should still download."""
        cfg = RepoConfig(
            name="owner/repo",
            destination=str(tmp_path / "raw"),
            within_hours=10,      # commit is 200h old — would normally skip
            normalize_for_rag=False,
        )
        sha, dt = self._old_commit()
        with patch(self._GET_LATEST, return_value=(sha, dt)), \
             patch(self._GET_TREE, return_value=[]), \
             patch(self._DOWNLOAD) as mock_dl:
            from bamboo_mcp_services.agents.github_doc_sync_agent.github_markdown_sync import sync_repo
            sync_repo(cfg)
        # _get_tree was called (not skipped), even though commit is old
        mock_dl.assert_not_called()   # no blobs, so no downloads — but we got past the gate

    def test_second_run_skips_old_commit(self, tmp_path):
        """State file present + within_hours exceeded → should skip."""
        import json
        dest_root = tmp_path / "raw"
        # State file lives inside the owner/repo subdirectory.
        per_repo_dest = dest_root / "owner" / "repo"
        per_repo_dest.mkdir(parents=True)
        state_path = per_repo_dest / ".sync_state.json"
        state_path.write_text(json.dumps({
            "last_commit_sha": "oldhash",
            "last_sync_time": "2026-01-01T00:00:00+00:00",
            "files_downloaded": 5,
        }))
        cfg = RepoConfig(
            name="owner/repo",
            destination=str(dest_root),
            within_hours=10,
            normalize_for_rag=False,
        )
        sha, dt = self._old_commit()
        with patch(self._GET_LATEST, return_value=(sha, dt)), \
             patch(self._GET_TREE) as mock_tree:
            from bamboo_mcp_services.agents.github_doc_sync_agent.github_markdown_sync import sync_repo
            sync_repo(cfg)
        mock_tree.assert_not_called()  # skipped before tree fetch


# ===========================================================================
# Generic git-clone sync path (git: true)
# ===========================================================================

class TestSyncGitRepo:
    """Tests for the generic git-clone sync path (git=True / clone_url)."""

    _SYNC_GIT = (
        "bamboo_mcp_services.agents.github_doc_sync_agent"
        ".github_markdown_sync.sync_git_repo"
    )
    _SYNC_WIKI = (
        "bamboo_mcp_services.agents.github_doc_sync_agent"
        ".github_markdown_sync.sync_wiki_repo"
    )

    def test_sync_repo_dispatches_to_git(self, tmp_path):
        """sync_repo with git=True must call sync_git_repo."""
        cfg = RepoConfig(
            name="simgrid/simgrid",
            destination=str(tmp_path),
            git=True,
            clone_url="https://framagit.org/simgrid/simgrid.git",
        )
        with patch(self._SYNC_GIT) as mock_git:
            from bamboo_mcp_services.agents.github_doc_sync_agent.github_markdown_sync import sync_repo
            sync_repo(cfg)
        mock_git.assert_called_once_with(cfg)

    def test_sync_repo_git_does_not_call_wiki(self, tmp_path):
        """sync_repo with git=True must NOT call sync_wiki_repo."""
        cfg = RepoConfig(
            name="simgrid/simgrid",
            destination=str(tmp_path),
            git=True,
            clone_url="https://framagit.org/simgrid/simgrid.git",
        )
        with patch(self._SYNC_GIT), patch(self._SYNC_WIKI) as mock_wiki:
            from bamboo_mcp_services.agents.github_doc_sync_agent.github_markdown_sync import sync_repo
            sync_repo(cfg)
        mock_wiki.assert_not_called()

    def test_sync_git_missing_clone_url_raises(self, tmp_path):
        """sync_git_repo must raise ValueError when clone_url is not set."""
        cfg = RepoConfig(
            name="owner/repo",
            destination=str(tmp_path),
            git=True,
        )
        from bamboo_mcp_services.agents.github_doc_sync_agent.github_markdown_sync import sync_git_repo
        with pytest.raises(ValueError, match="clone_url"):
            sync_git_repo(cfg)

    def test_git_false_does_not_dispatch_to_git(self, tmp_path):
        """sync_repo with git=False must NOT call sync_git_repo."""
        cfg = RepoConfig(name="owner/repo", destination=str(tmp_path))
        _GET_LATEST = (
            "bamboo_mcp_services.agents.github_doc_sync_agent"
            ".github_markdown_sync.get_latest_commit"
        )
        _GET_TREE = (
            "bamboo_mcp_services.agents.github_doc_sync_agent"
            ".github_markdown_sync._get_tree"
        )
        with patch(self._SYNC_GIT) as mock_git, \
             patch(_GET_LATEST, return_value=("abc123", datetime.now(tz=timezone.utc))), \
             patch(_GET_TREE, return_value=[]):
            from bamboo_mcp_services.agents.github_doc_sync_agent.github_markdown_sync import sync_repo
            sync_repo(cfg)
        mock_git.assert_not_called()

    def test_load_config_reads_git_and_clone_url(self, tmp_path):
        """load_config must parse git: true and clone_url from YAML."""
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(
            "repos:\n"
            "  - name: simgrid/simgrid\n"
            "    destination: /tmp/raw\n"
            "    git: true\n"
            "    clone_url: https://framagit.org/simgrid/simgrid.git\n"
            "    branch: master\n"
        )
        from bamboo_mcp_services.agents.github_doc_sync_agent.github_markdown_sync import load_config
        repos, _ = load_config(cfg_file)
        assert len(repos) == 1
        assert repos[0].git is True
        assert repos[0].clone_url == "https://framagit.org/simgrid/simgrid.git"
        assert repos[0].branch == "master"

    def test_load_config_git_defaults_false(self, tmp_path):
        """load_config must default git to False when not specified."""
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(
            "repos:\n"
            "  - name: owner/repo\n"
            "    destination: /tmp/raw\n"
        )
        from bamboo_mcp_services.agents.github_doc_sync_agent.github_markdown_sync import load_config
        repos, _ = load_config(cfg_file)
        assert repos[0].git is False
        assert repos[0].clone_url is None

    def test_git_files_copied_and_normalized(self, tmp_path):
        """sync_git_repo copies matching files and writes normalized output."""
        dest = tmp_path / "raw"
        norm_dest = tmp_path / "rag"
        cfg = RepoConfig(
            name="simgrid/simgrid",
            destination=str(dest),
            normalized_destination=str(norm_dest),
            include_patterns=["docs/source/*.rst"],
            normalize_for_rag=True,
            git=True,
            clone_url="https://framagit.org/simgrid/simgrid.git",
            branch="master",
        )

        # Build a fake clone with one matching and one non-matching file.
        clone_target = tmp_path / "clone_base" / "clone"
        (clone_target / ".git").mkdir(parents=True)
        (clone_target / "docs" / "source").mkdir(parents=True)
        (clone_target / "docs" / "source" / "index.rst").write_text(
            "SimGrid Docs\n============\n", encoding="utf-8"
        )
        (clone_target / "README.md").write_text("not matched", encoding="utf-8")

        fake_sha = "cafebabe" * 5
        _GIT_CLONE_HEAD_SHA = (
            "bamboo_mcp_services.agents.github_doc_sync_agent"
            ".github_markdown_sync._git_clone_head_sha"
        )
        _GIT_CLONE_HEAD_DT = (
            "bamboo_mcp_services.agents.github_doc_sync_agent"
            ".github_markdown_sync._git_clone_head_datetime"
        )

        import subprocess as _sp

        completed = _sp.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        class FakeTmpDir:
            def __enter__(self):
                return str(tmp_path / "clone_base")

            def __exit__(self, *a):
                pass

        from bamboo_mcp_services.agents.github_doc_sync_agent.github_markdown_sync import sync_git_repo
        with patch("subprocess.run", return_value=completed), \
             patch("tempfile.TemporaryDirectory", return_value=FakeTmpDir()), \
             patch(_GIT_CLONE_HEAD_SHA, return_value=fake_sha), \
             patch(_GIT_CLONE_HEAD_DT, return_value=datetime.now(tz=timezone.utc)):
            sync_git_repo(cfg)

        out_file = dest / "simgrid" / "simgrid" / "docs" / "source" / "index.rst"
        assert out_file.exists(), "index.rst should be copied to dest"
        readme = dest / "simgrid" / "simgrid" / "README.md"
        assert not readme.exists(), "README.md should not be copied (not in include_patterns)"
        norm_file = norm_dest / "simgrid" / "simgrid" / "docs" / "source" / "index.rst"
        assert norm_file.exists(), "index.rst should be normalized to norm_dest"
        norm_text = norm_file.read_text()
        assert "source_repo: simgrid/simgrid" in norm_text

    def test_git_branch_passed_to_clone(self, tmp_path):
        """sync_git_repo must pass -b {branch} to git clone when branch is set."""
        cfg = RepoConfig(
            name="simgrid/simgrid",
            destination=str(tmp_path),
            git=True,
            clone_url="https://framagit.org/simgrid/simgrid.git",
            branch="master",
        )
        import subprocess as _sp

        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return _sp.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        clone_target = tmp_path / "clone_base" / "clone"
        clone_target.mkdir(parents=True)
        (clone_target / ".git").mkdir()

        _GIT_CLONE_HEAD_SHA = (
            "bamboo_mcp_services.agents.github_doc_sync_agent"
            ".github_markdown_sync._git_clone_head_sha"
        )
        _GIT_CLONE_HEAD_DT = (
            "bamboo_mcp_services.agents.github_doc_sync_agent"
            ".github_markdown_sync._git_clone_head_datetime"
        )

        class FakeTmpDir:
            def __enter__(self):
                return str(tmp_path / "clone_base")

            def __exit__(self, *a):
                pass

        from bamboo_mcp_services.agents.github_doc_sync_agent.github_markdown_sync import sync_git_repo
        with patch("subprocess.run", side_effect=fake_run), \
             patch("tempfile.TemporaryDirectory", return_value=FakeTmpDir()), \
             patch(_GIT_CLONE_HEAD_SHA, return_value="aabbcc"), \
             patch(_GIT_CLONE_HEAD_DT, return_value=datetime.now(tz=timezone.utc)):
            sync_git_repo(cfg)

        clone_call = calls[0]
        assert "-b" in clone_call
        assert "master" in clone_call
        assert "https://framagit.org/simgrid/simgrid.git" in clone_call

    def test_git_second_run_skips_unchanged_sha(self, tmp_path):
        """sync_git_repo with matching SHA must skip without copying files."""
        import json
        dest_root = tmp_path / "raw"
        per_repo = dest_root / "simgrid" / "simgrid"
        per_repo.mkdir(parents=True)
        state_path = per_repo / ".sync_state.json"
        known_sha = "deadbeef" * 5
        state_path.write_text(json.dumps({
            "last_commit_sha": known_sha,
            "last_sync_time": "2026-01-01T00:00:00+00:00",
            "files_downloaded": 10,
        }))
        cfg = RepoConfig(
            name="simgrid/simgrid",
            destination=str(dest_root),
            git=True,
            clone_url="https://framagit.org/simgrid/simgrid.git",
        )
        import subprocess as _sp
        completed = _sp.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        clone_target = tmp_path / "clone_base" / "clone"
        clone_target.mkdir(parents=True)
        (clone_target / ".git").mkdir()

        _GIT_CLONE_HEAD_SHA = (
            "bamboo_mcp_services.agents.github_doc_sync_agent"
            ".github_markdown_sync._git_clone_head_sha"
        )
        _GIT_CLONE_HEAD_DT = (
            "bamboo_mcp_services.agents.github_doc_sync_agent"
            ".github_markdown_sync._git_clone_head_datetime"
        )

        class FakeTmpDir:
            def __enter__(self):
                return str(tmp_path / "clone_base")

            def __exit__(self, *a):
                pass

        from bamboo_mcp_services.agents.github_doc_sync_agent.github_markdown_sync import sync_git_repo
        with patch("subprocess.run", return_value=completed), \
             patch("tempfile.TemporaryDirectory", return_value=FakeTmpDir()), \
             patch(_GIT_CLONE_HEAD_SHA, return_value=known_sha), \
             patch(_GIT_CLONE_HEAD_DT, return_value=datetime.now(tz=timezone.utc)):
            sync_git_repo(cfg)

        # State file must be unchanged.
        saved = json.loads(state_path.read_text())
        assert saved["files_downloaded"] == 10
