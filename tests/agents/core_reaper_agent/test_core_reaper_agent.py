"""Tests for the core-dump workspace reaper.

Covers:
- Manifest parsing, including unknown versions and corrupt payloads
- The four safety rules (terminal state, age, slot ownership, worker liveness)
- The path guard, one test per rejection reason
- The no-deletion invariant, enforced by an AST scan of the module source
- Full sweeps: prune/purge targets, orphans, pressure pass, unmanaged entries
- CLI argument handling and exit codes
"""
from __future__ import annotations

import ast
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bamboo_mcp_services.agents.base import AgentState
from bamboo_mcp_services.agents.core_reaper_agent import agent as reaper
from bamboo_mcp_services.agents.core_reaper_agent.agent import (
    Action,
    CoreReaperAgent,
    CoreReaperConfig,
    JOB_SUBDIR,
    LOCK_NAME,
    MANIFEST_NAME,
    Manifest,
    UnsafePathError,
    assert_reclaimable_path,
    classify,
    directory_usage_bytes,
    human_bytes,
    is_pid_alive,
    parse_iso_utc,
    read_slot_holder,
    resolve_quota_bytes,
    resolve_root,
)
from bamboo_mcp_services.agents.core_reaper_agent.cli import (
    EXIT_ERRORS,
    EXIT_OK,
    EXIT_PRESSURE,
    EXIT_USAGE,
    build_parser,
    main,
)

NOW = datetime(2026, 8, 24, 12, 0, 0, tzinfo=timezone.utc)

#: A PID that is essentially certain not to exist.
DEAD_PID = 999_999


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iso(when: datetime) -> str:
    """Format a datetime the way the manifest does.

    Args:
        when: Timestamp to format.

    Returns:
        ISO-8601 string with a trailing ``Z``.
    """
    return when.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def make_workspace(
    root: Path,
    job_id: str = "7272161793",
    state: str = "complete",
    age_hours: float = 5.0,
    worker_pid=None,
    core_bytes: int = 4096,
    manifest: bool = True,
    manifest_version: int = 1,
    job_dir: bool = True,
    extra_clutter: bool = True,
) -> Path:
    """Create a workspace on disk that mirrors the real layout.

    Args:
        root: Analysis root.
        job_id: PanDA job ID.
        state: Manifest state.
        age_hours: How long ago the run finished.
        worker_pid: Value for the ``worker_pid`` field.
        core_bytes: Size of the fake core file inside ``job/``.
        manifest: Whether to write a manifest at all.
        manifest_version: Value for ``manifest_version``.
        job_dir: Whether to create ``job/``.
        extra_clutter: Whether to drop a stray ``probe.sh`` in the workspace.

    Returns:
        Path to the created workspace.
    """
    workspace = root / f"job-{job_id}"
    workspace.mkdir(parents=True, exist_ok=True)
    finished = NOW - timedelta(hours=age_hours)

    if job_dir:
        job = workspace / JOB_SUBDIR
        job.mkdir(exist_ok=True)
        (job / "core.1178643").write_bytes(b"\0" * core_bytes)
        (job / "payload.stdout").write_text("stdout\n", encoding="utf-8")

    for name in ("evidence.json", "gdb_raw.txt", "worker.log"):
        (workspace / name).write_text(f"{name} content\n", encoding="utf-8")
    if extra_clutter:
        (workspace / "probe.sh").write_text("#!/bin/sh\n", encoding="utf-8")

    if manifest:
        payload = {
            "manifest_version": manifest_version,
            "job_id": job_id,
            "request_id": f"req-{job_id}",
            "state": state,
            "created_utc": _iso(finished - timedelta(hours=1)),
            "updated_utc": _iso(finished),
            "finished_utc": _iso(finished) if state in ("complete", "failed") else None,
            "worker_pid": worker_pid,
            "bytes_downloaded": core_bytes,
            "error": None,
        }
        (workspace / MANIFEST_NAME).write_text(json.dumps(payload), encoding="utf-8")

    _backdate(workspace, finished)
    return workspace


def _backdate(workspace: Path, when: datetime) -> None:
    """Set mtimes throughout a workspace so orphan ageing is testable.

    Args:
        workspace: Workspace directory.
        when: Timestamp to apply.
    """
    stamp = when.timestamp()
    paths = sorted(workspace.rglob("*"), reverse=True) + [workspace]
    for path in paths:
        os.utime(path, (stamp, stamp))


def make_config(root: Path, **overrides) -> CoreReaperConfig:
    """Build a config for tests.

    Args:
        root: Analysis root.
        **overrides: Fields to override.

    Returns:
        A :class:`CoreReaperConfig`.
    """
    defaults = {
        "root": root,
        "min_age_hours": 1.0,
        "orphan_age_hours": 24.0,
        "quota_bytes": 1_000_000,
        "pressure_pct": 80.0,
        "target_pct": 60.0,
        "min_age_floor_hours": 0.5,
    }
    defaults.update(overrides)
    return CoreReaperConfig(**defaults)


def make_agent(root: Path, monkeypatch: pytest.MonkeyPatch, **overrides) -> CoreReaperAgent:
    """Build a started agent with the allowlist pointed at *root*.

    The allowlist is hardcoded to ``/tmp/bamboo`` in production; tests widen it
    for the duration of a test only, which is the sole supported way to do so.

    Args:
        root: Analysis root (typically pytest's ``tmp_path``).
        monkeypatch: pytest monkeypatch fixture.
        **overrides: Config field overrides.

    Returns:
        A started :class:`CoreReaperAgent`.
    """
    monkeypatch.setattr(reaper, "ALLOWED_PREFIXES", (str(root),))
    agent = CoreReaperAgent(config=make_config(root, **overrides))
    agent.start()
    return agent


def write_lock(root: Path, payload) -> None:
    """Write the ownership record into ``.busy.lock``.

    Args:
        root: Analysis root.
        payload: Object to serialise, e.g. ``{}`` or ``{"job_id": "1"}``.
    """
    (root / LOCK_NAME).write_text(json.dumps(payload), encoding="utf-8")


def load_workspace(agent: CoreReaperAgent, path: Path):
    """Build a Workspace object through the agent's own reader.

    Args:
        agent: Agent to use.
        path: Workspace directory.

    Returns:
        The :class:`~bamboo_mcp_services.agents.core_reaper_agent.agent.Workspace`.
    """
    return agent._build_workspace(path)


def snapshot_tree(root: Path):
    """Capture every path, size and mtime under *root*.

    Args:
        root: Directory to snapshot.

    Returns:
        Sorted list of (relative path, size, mtime) tuples.
    """
    entries = []
    for path in sorted(root.rglob("*")):
        stat = path.lstat()
        entries.append((str(path.relative_to(root)), stat.st_size, stat.st_mtime))
    return entries


# ---------------------------------------------------------------------------
# The no-deletion invariant
# ---------------------------------------------------------------------------


class TestNoDeletionInvariant:
    """This build must be incapable of removing anything.

    These tests scan ``agent.py``'s abstract syntax tree rather than its text,
    so the module docstring is free to name the forbidden calls while the code
    is held to not making them.  If a future version gains an ``--apply``
    flag, these tests are the deliberate gate to walk through.
    """

    #: Method names that would remove or truncate data.
    BLOCKED_METHODS = frozenset(
        {"rmtree", "unlink", "rmdir", "removedirs", "remove", "truncate", "system", "popen", "rename"}
    )

    #: Modules that must not be imported by the reaper.
    BLOCKED_IMPORTS = frozenset({"shutil", "subprocess"})

    @staticmethod
    def _tree() -> ast.Module:
        """Parse the reaper module source.

        Returns:
            The parsed AST.
        """
        source = Path(reaper.__file__).read_text(encoding="utf-8")
        return ast.parse(source)

    def test_no_destructive_calls(self):
        """No call to a removing or truncating method appears anywhere."""
        offenders = []
        for node in ast.walk(self._tree()):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in self.BLOCKED_METHODS:
                    offenders.append((node.func.attr, node.lineno))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in self.BLOCKED_METHODS:
                    offenders.append((node.func.id, node.lineno))
        assert offenders == [], f"destructive call(s) found in agent.py: {offenders}"

    def test_no_os_replace(self):
        """``os.replace`` is absent, even though ``datetime.replace`` is allowed."""
        offenders = [
            node.lineno
            for node in ast.walk(self._tree())
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "os"
            and node.attr in {"replace", "rename", "remove", "unlink", "rmdir", "removedirs", "truncate"}
        ]
        assert offenders == [], f"os-level mutation found at line(s) {offenders}"

    def test_no_destructive_imports(self):
        """``shutil`` and ``subprocess`` are not imported."""
        imported = set()
        for node in ast.walk(self._tree()):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert not (imported & self.BLOCKED_IMPORTS)

    def test_no_writable_open_calls(self):
        """Every ``open()`` in the module is read-only."""
        offenders = []
        for node in ast.walk(self._tree()):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "open"):
                continue
            modes = [arg.value for arg in node.args[1:2] if isinstance(arg, ast.Constant)]
            modes += [kw.value.value for kw in node.keywords if kw.arg == "mode" and isinstance(kw.value, ast.Constant)]
            for mode in modes:
                if any(flag in str(mode) for flag in ("w", "a", "x", "+")):
                    offenders.append((mode, node.lineno))
        assert offenders == [], f"writable open() found: {offenders}"

    def test_os_open_is_read_only(self):
        """``os.open`` is only ever called with ``O_RDONLY``."""
        for node in ast.walk(self._tree()):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "open"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "os"
            ):
                flags = [ast.unparse(arg) for arg in node.args[1:]]
                assert flags == ["os.O_RDONLY"], f"os.open with flags {flags} at line {node.lineno}"

    def test_sweep_leaves_the_tree_untouched(self, tmp_path, monkeypatch):
        """A full sweep changes nothing on disk, byte for byte."""
        make_workspace(tmp_path, job_id="1", age_hours=99.0)
        make_workspace(tmp_path, job_id="2", state="failed", age_hours=99.0)
        make_workspace(tmp_path, job_id="3", manifest=False, age_hours=99.0)
        write_lock(tmp_path, {})

        before = snapshot_tree(tmp_path)
        agent = make_agent(tmp_path, monkeypatch)
        report = agent.sweep(now=NOW)
        agent.stop()

        assert report.candidates, "expected the sweep to find something to report"
        assert snapshot_tree(tmp_path) == before

    def test_lock_file_is_never_created(self, tmp_path, monkeypatch):
        """A missing ``.busy.lock`` stays missing."""
        make_workspace(tmp_path, job_id="1", age_hours=99.0)
        agent = make_agent(tmp_path, monkeypatch)
        agent.sweep(now=NOW)
        assert not (tmp_path / LOCK_NAME).exists()


# ---------------------------------------------------------------------------
# Manifest parsing
# ---------------------------------------------------------------------------


class TestManifest:
    """Manifest reading is total: every failure yields a reason code."""

    def test_parses_a_valid_manifest(self, tmp_path):
        """All relevant fields are read."""
        workspace = make_workspace(tmp_path, job_id="42", worker_pid=4321)
        manifest = Manifest.load(workspace / MANIFEST_NAME)
        assert manifest.readable
        assert manifest.job_id == "42"
        assert manifest.state == "complete"
        assert manifest.worker_pid == 4321
        assert manifest.reference_time is not None

    def test_missing_manifest(self, tmp_path):
        """An absent file is reported as ``no-manifest``."""
        manifest = Manifest.load(tmp_path / MANIFEST_NAME)
        assert not manifest.readable
        assert manifest.problem == "no-manifest"

    def test_corrupt_manifest(self, tmp_path):
        """Malformed JSON is reported, not raised."""
        (tmp_path / MANIFEST_NAME).write_text("{not json", encoding="utf-8")
        manifest = Manifest.load(tmp_path / MANIFEST_NAME)
        assert manifest.problem == "manifest-unparseable"

    def test_non_object_manifest(self, tmp_path):
        """A JSON array is not a manifest."""
        (tmp_path / MANIFEST_NAME).write_text("[1, 2, 3]", encoding="utf-8")
        assert Manifest.load(tmp_path / MANIFEST_NAME).problem == "manifest-unparseable"

    def test_unknown_manifest_version(self, tmp_path):
        """An unknown schema version is refused rather than guessed at."""
        workspace = make_workspace(tmp_path, job_id="9", manifest_version=2)
        manifest = Manifest.load(workspace / MANIFEST_NAME)
        assert not manifest.readable
        assert manifest.problem == "manifest-version-unsupported"

    def test_reference_time_falls_back_to_updated(self):
        """``updated_utc`` is used when ``finished_utc`` is absent."""
        manifest = Manifest(readable=True, updated_utc=NOW)
        assert manifest.reference_time == NOW

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("2026-08-24T12:00:00Z", NOW),
            ("2026-08-24T12:00:00+00:00", NOW),
            ("2026-08-24T12:00:00", NOW),
            ("not a date", None),
            ("", None),
            (None, None),
            (17, None),
        ],
    )
    def test_parse_iso_utc(self, value, expected):
        """Timestamps parse tolerantly, naive values are assumed UTC."""
        assert parse_iso_utc(value) == expected


# ---------------------------------------------------------------------------
# The four safety rules
# ---------------------------------------------------------------------------


class TestClassification:
    """Classification implements the safety rules, independently."""

    def _classify(self, tmp_path, monkeypatch, holder=None, now=NOW, **kwargs):
        """Build one workspace and classify it.

        Args:
            tmp_path: Temporary directory.
            monkeypatch: pytest fixture.
            holder: Slot holder job ID.
            now: Current time.
            **kwargs: Passed to :func:`make_workspace`.

        Returns:
            The resulting Decision.
        """
        path = make_workspace(tmp_path, **kwargs)
        agent = make_agent(tmp_path, monkeypatch)
        return classify(load_workspace(agent, path), holder, now, agent.config)

    @pytest.mark.parametrize("state", ["complete", "failed"])
    def test_terminal_and_aged_is_reclaimable(self, tmp_path, monkeypatch, state):
        """Terminal state plus sufficient age is the happy path."""
        decision = self._classify(tmp_path, monkeypatch, state=state, age_hours=5.0)
        assert decision.action is Action.PRUNE_JOB_DIR
        assert decision.reason == f"terminal-and-aged:{state}"
        assert decision.target.name == JOB_SUBDIR

    @pytest.mark.parametrize("state", ["queued", "preparing", "downloading", "analyzing"])
    def test_non_terminal_states_are_skipped(self, tmp_path, monkeypatch, state):
        """A run that may still be in progress is never a candidate."""
        decision = self._classify(tmp_path, monkeypatch, state=state, age_hours=500.0)
        assert decision.action is Action.SKIP
        assert decision.reason.startswith("non-terminal-state:")

    def test_dead_worker_in_non_terminal_state_is_still_skipped(self, tmp_path, monkeypatch):
        """Rules 1 and 4 are not interchangeable — reconciliation is not ours."""
        decision = self._classify(
            tmp_path, monkeypatch, state="downloading", age_hours=500.0, worker_pid=DEAD_PID
        )
        assert decision.action is Action.SKIP

    def test_live_worker_in_terminal_state_is_skipped(self, tmp_path, monkeypatch):
        """A stale manifest with a live PID blocks reclaiming."""
        decision = self._classify(
            tmp_path, monkeypatch, state="complete", age_hours=500.0, worker_pid=os.getpid()
        )
        assert decision.action is Action.SKIP
        assert decision.reason == "worker-alive"

    def test_null_worker_pid_is_fine(self, tmp_path, monkeypatch):
        """A null PID satisfies rule 4."""
        decision = self._classify(tmp_path, monkeypatch, worker_pid=None, age_hours=5.0)
        assert decision.action is Action.PRUNE_JOB_DIR

    def test_too_young_is_deferred_not_refused(self, tmp_path, monkeypatch):
        """Only the age rule failed, so the workspace is pressure-eligible."""
        decision = self._classify(tmp_path, monkeypatch, age_hours=0.75)
        assert decision.action is Action.SKIP
        assert decision.reason == "too-young"
        assert decision.young_only is True

    def test_age_boundary(self, tmp_path, monkeypatch):
        """Exactly at the retention age counts as old enough."""
        decision = self._classify(tmp_path, monkeypatch, age_hours=1.0)
        assert decision.action is Action.PRUNE_JOB_DIR

    def test_slot_holder_is_never_touched(self, tmp_path, monkeypatch):
        """Rule 3: the workspace holding the slot is off limits."""
        decision = self._classify(tmp_path, monkeypatch, job_id="55", holder="55", age_hours=500.0)
        assert decision.action is Action.SKIP
        assert decision.reason == "holds-slot"

    def test_slot_holder_wins_over_missing_manifest(self, tmp_path, monkeypatch):
        """An orphan that holds the slot is still off limits."""
        decision = self._classify(
            tmp_path, monkeypatch, job_id="55", holder="55", manifest=False, age_hours=500.0
        )
        assert decision.reason == "holds-slot"

    def test_unknown_manifest_version_blocks(self, tmp_path, monkeypatch):
        """Unknown schema versions are skipped with their own reason code."""
        decision = self._classify(tmp_path, monkeypatch, manifest_version=7, age_hours=500.0)
        assert decision.reason == "manifest-version-unsupported"

    def test_missing_timestamps_block(self, tmp_path, monkeypatch):
        """A manifest with no usable timestamp cannot be aged."""
        workspace = make_workspace(tmp_path, job_id="8", age_hours=500.0)
        payload = json.loads((workspace / MANIFEST_NAME).read_text(encoding="utf-8"))
        payload["finished_utc"] = None
        payload["updated_utc"] = None
        (workspace / MANIFEST_NAME).write_text(json.dumps(payload), encoding="utf-8")
        agent = make_agent(tmp_path, monkeypatch)
        decision = classify(load_workspace(agent, workspace), None, NOW, agent.config)
        assert decision.reason == "no-timestamp"

    def test_already_pruned_workspace_is_skipped(self, tmp_path, monkeypatch):
        """Prune mode is idempotent: no ``job/`` means nothing to do."""
        decision = self._classify(tmp_path, monkeypatch, job_dir=False, age_hours=500.0)
        assert decision.action is Action.SKIP
        assert decision.reason == "already-pruned"

    def test_symlinked_job_dir_is_not_mistaken_for_pruned(self, tmp_path, monkeypatch):
        """A symlinked ``job/`` gets its own reason code, not 'already-pruned'."""
        workspace = make_workspace(tmp_path, job_id="6", age_hours=500.0, job_dir=False)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (workspace / JOB_SUBDIR).symlink_to(elsewhere, target_is_directory=True)
        agent = make_agent(tmp_path, monkeypatch)
        decision = classify(load_workspace(agent, workspace), None, NOW, agent.config)
        assert decision.action is Action.SKIP
        assert decision.reason == "job-dir-symlink"

    def test_symlinked_job_dir_survives_a_full_sweep(self, tmp_path, monkeypatch):
        """End to end: nothing is reported for a workspace with a symlinked job/."""
        workspace = make_workspace(tmp_path, job_id="6", age_hours=500.0, job_dir=False)
        (workspace / JOB_SUBDIR).symlink_to(tmp_path / "elsewhere", target_is_directory=True)
        agent = make_agent(tmp_path, monkeypatch)
        report = agent.sweep(now=NOW)
        assert report.candidates == []
        assert report.skipped["job-dir-symlink"] == 1

    def test_purge_mode_targets_the_workspace(self, tmp_path, monkeypatch):
        """Purge mode reports the whole directory."""
        path = make_workspace(tmp_path, job_id="3", age_hours=500.0)
        agent = make_agent(tmp_path, monkeypatch, mode="purge")
        decision = classify(load_workspace(agent, path), None, NOW, agent.config)
        assert decision.action is Action.REMOVE_WORKSPACE
        assert decision.target == path

    def test_failed_escalation(self, tmp_path, monkeypatch):
        """Old failed runs escalate to whole-workspace removal when configured."""
        path = make_workspace(tmp_path, job_id="4", state="failed", age_hours=200.0)
        agent = make_agent(tmp_path, monkeypatch, purge_failed_after_hours=168.0)
        decision = classify(load_workspace(agent, path), None, NOW, agent.config)
        assert decision.action is Action.REMOVE_WORKSPACE

    def test_failed_escalation_respects_its_threshold(self, tmp_path, monkeypatch):
        """A recent failed run is still only pruned."""
        path = make_workspace(tmp_path, job_id="4", state="failed", age_hours=10.0)
        agent = make_agent(tmp_path, monkeypatch, purge_failed_after_hours=168.0)
        decision = classify(load_workspace(agent, path), None, NOW, agent.config)
        assert decision.action is Action.PRUNE_JOB_DIR

    def test_complete_runs_never_escalate(self, tmp_path, monkeypatch):
        """The escalation applies to failed runs only."""
        path = make_workspace(tmp_path, job_id="4", state="complete", age_hours=900.0)
        agent = make_agent(tmp_path, monkeypatch, purge_failed_after_hours=168.0)
        decision = classify(load_workspace(agent, path), None, NOW, agent.config)
        assert decision.action is Action.PRUNE_JOB_DIR


class TestOrphans:
    """Workspaces with no manifest get a longer, distinctly-logged threshold."""

    def test_young_orphan_is_kept(self, tmp_path, monkeypatch):
        """Below the orphan age, an unmanifested workspace is left alone."""
        path = make_workspace(tmp_path, job_id="1", manifest=False, age_hours=5.0)
        agent = make_agent(tmp_path, monkeypatch)
        decision = classify(load_workspace(agent, path), None, NOW, agent.config)
        assert decision.action is Action.SKIP
        assert decision.reason == "orphan-too-young"

    def test_old_orphan_is_reported_whole(self, tmp_path, monkeypatch):
        """Past the orphan age the whole workspace is reported."""
        path = make_workspace(tmp_path, job_id="1", manifest=False, age_hours=100.0)
        agent = make_agent(tmp_path, monkeypatch)
        decision = classify(load_workspace(agent, path), None, NOW, agent.config)
        assert decision.action is Action.REMOVE_WORKSPACE
        assert decision.reason == "orphan-no-manifest"

    def test_orphan_age_is_longer_than_the_normal_age(self, tmp_path, monkeypatch):
        """An orphan older than min_age but younger than orphan_age is kept."""
        path = make_workspace(tmp_path, job_id="1", manifest=False, age_hours=12.0)
        agent = make_agent(tmp_path, monkeypatch, min_age_hours=1.0, orphan_age_hours=24.0)
        decision = classify(load_workspace(agent, path), None, NOW, agent.config)
        assert decision.action is Action.SKIP

    def test_orphans_are_not_pressure_eligible(self, tmp_path, monkeypatch):
        """Ambiguous workspaces never get their threshold relaxed by pressure."""
        path = make_workspace(tmp_path, job_id="1", manifest=False, age_hours=12.0)
        agent = make_agent(tmp_path, monkeypatch)
        decision = classify(load_workspace(agent, path), None, NOW, agent.config)
        assert decision.young_only is False


# ---------------------------------------------------------------------------
# The lock
# ---------------------------------------------------------------------------


class TestSlotLock:
    """Slot ownership is the file's content, not the flock."""

    def test_holder_is_read_from_content(self, tmp_path):
        """The ownership record names the holder."""
        write_lock(tmp_path, {"request_id": "r1", "job_id": "7272161793"})
        assert read_slot_holder(tmp_path) == "7272161793"

    def test_empty_object_means_free(self, tmp_path):
        """``{}`` is how Bamboo MCP releases the slot."""
        write_lock(tmp_path, {})
        assert read_slot_holder(tmp_path) is None

    def test_missing_lock_file_means_free(self, tmp_path):
        """No lock file is not an error, and none is created."""
        assert read_slot_holder(tmp_path) is None
        assert not (tmp_path / LOCK_NAME).exists()

    def test_garbage_content_means_free(self, tmp_path):
        """Unparseable content degrades to 'no holder' with a warning."""
        (tmp_path / LOCK_NAME).write_text("<<<not json>>>", encoding="utf-8")
        assert read_slot_holder(tmp_path) is None

    def test_empty_file_means_free(self, tmp_path):
        """A zero-length lock file is treated as free."""
        (tmp_path / LOCK_NAME).write_text("", encoding="utf-8")
        assert read_slot_holder(tmp_path) is None

    def test_holder_is_excluded_from_the_sweep(self, tmp_path, monkeypatch):
        """End to end: the held workspace produces no candidate."""
        make_workspace(tmp_path, job_id="111", age_hours=500.0)
        make_workspace(tmp_path, job_id="222", age_hours=500.0)
        write_lock(tmp_path, {"request_id": "r", "job_id": "111"})
        agent = make_agent(tmp_path, monkeypatch)
        report = agent.sweep(now=NOW)
        assert [c["job_id"] for c in report.candidates] == ["222"]
        assert report.skipped["holds-slot"] == 1


# ---------------------------------------------------------------------------
# The path guard
# ---------------------------------------------------------------------------


class TestPathGuard:
    """One test per rejection reason; the guard is the single choke point."""

    def test_accepts_a_normal_job_dir(self, tmp_path):
        """The happy path passes."""
        workspace = make_workspace(tmp_path, job_id="1")
        assert_reclaimable_path(workspace / JOB_SUBDIR, tmp_path, "job_dir", (str(tmp_path),))

    def test_accepts_a_normal_workspace(self, tmp_path):
        """A whole workspace passes in purge mode."""
        workspace = make_workspace(tmp_path, job_id="1")
        assert_reclaimable_path(workspace, tmp_path, "workspace", (str(tmp_path),))

    def test_rejects_paths_outside_the_allowlist(self, tmp_path):
        """The hardcoded allowlist is the outermost check."""
        workspace = make_workspace(tmp_path, job_id="1")
        with pytest.raises(UnsafePathError, match="outside-allowlist"):
            assert_reclaimable_path(workspace, tmp_path, "workspace", ("/tmp/bamboo",))

    def test_rejects_paths_outside_the_root(self, tmp_path):
        """Containment under the configured root is required."""
        root = tmp_path / "root"
        other = tmp_path / "other"
        root.mkdir()
        other.mkdir()
        (other / "job-1").mkdir()
        with pytest.raises(UnsafePathError, match="not-under-root"):
            assert_reclaimable_path(other / "job-1", root, "workspace", (str(tmp_path),))

    def test_rejects_the_root_itself(self, tmp_path):
        """The root can never be a target."""
        with pytest.raises(UnsafePathError):
            assert_reclaimable_path(tmp_path, tmp_path, "workspace", (str(tmp_path),))

    def test_rejects_a_symlinked_job_dir(self, tmp_path):
        """``job/`` is built from a remote listing; a symlink there is fatal."""
        workspace = make_workspace(tmp_path, job_id="1", job_dir=False)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (workspace / JOB_SUBDIR).symlink_to(elsewhere, target_is_directory=True)
        with pytest.raises(UnsafePathError, match="symlink-in-path"):
            assert_reclaimable_path(workspace / JOB_SUBDIR, tmp_path, "job_dir", (str(tmp_path),))

    def test_rejects_a_symlinked_intermediate_component(self, tmp_path):
        """A symlinked workspace cannot redirect the target either."""
        real = tmp_path / "real-job-1"
        real.mkdir()
        (real / JOB_SUBDIR).mkdir()
        link = tmp_path / "job-1"
        link.symlink_to(real, target_is_directory=True)
        with pytest.raises(UnsafePathError, match="symlink-in-path"):
            assert_reclaimable_path(link / JOB_SUBDIR, tmp_path, "job_dir", (str(tmp_path),))

    def test_rejects_escaping_symlink_targets(self, tmp_path):
        """A symlink pointing outside the root escapes both checks."""
        root = tmp_path / "root"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (root / "job-1").symlink_to(outside, target_is_directory=True)
        with pytest.raises(UnsafePathError):
            assert_reclaimable_path(root / "job-1", root, "workspace", (str(tmp_path),))

    @pytest.mark.parametrize("name", ["notajob", "job-abc", "job-", "core.1234"])
    def test_rejects_wrong_workspace_shapes(self, tmp_path, name):
        """Only ``job-<digits>`` is a workspace."""
        (tmp_path / name).mkdir()
        with pytest.raises(UnsafePathError, match="not-a-workspace-name"):
            assert_reclaimable_path(tmp_path / name, tmp_path, "workspace", (str(tmp_path),))

    def test_rejects_wrong_job_dir_shape(self, tmp_path):
        """A prune target must literally be ``<job-N>/job``."""
        workspace = make_workspace(tmp_path, job_id="1")
        with pytest.raises(UnsafePathError, match="not-a-job-dir"):
            assert_reclaimable_path(workspace / "workDir", tmp_path, "job_dir", (str(tmp_path),))

    @pytest.mark.parametrize("name", ["worker.log", "evidence.json", "gdb_raw.txt", MANIFEST_NAME, LOCK_NAME])
    def test_rejects_reserved_names(self, tmp_path, name):
        """The diagnostic record and the lock can never be targets."""
        workspace = make_workspace(tmp_path, job_id="1")
        with pytest.raises(UnsafePathError):
            assert_reclaimable_path(workspace / name, tmp_path, "workspace", (str(tmp_path),))

    def test_rejects_unknown_kinds(self, tmp_path):
        """An unrecognised target kind is a programming error, not a default."""
        workspace = make_workspace(tmp_path, job_id="1")
        with pytest.raises(UnsafePathError, match="unknown-target-kind"):
            assert_reclaimable_path(workspace, tmp_path, "everything", (str(tmp_path),))

    def test_rejects_forbidden_targets(self):
        """``/tmp/bamboo`` itself is never a target."""
        with pytest.raises(UnsafePathError):
            assert_reclaimable_path(Path("/tmp/bamboo"), Path("/tmp"), "workspace", ("/tmp",))

    def test_default_allowlist_is_tmp_bamboo(self):
        """The production default is hardcoded and narrow."""
        assert reaper.ALLOWED_PREFIXES == ("/tmp/bamboo",)

    def test_root_outside_the_allowlist_refuses_every_candidate(self, tmp_path, monkeypatch):
        """A misconfigured root yields refusals, not reclaims."""
        make_workspace(tmp_path, job_id="1", age_hours=500.0)
        monkeypatch.setattr(reaper, "ALLOWED_PREFIXES", ("/tmp/bamboo",))
        agent = CoreReaperAgent(config=make_config(tmp_path))
        agent.start()
        report = agent.sweep(now=NOW)
        assert report.candidates == []
        assert report.refused and "outside-allowlist" in report.refused[0]["guard"]
        assert report.reclaimable_bytes == 0


# ---------------------------------------------------------------------------
# Sweeps
# ---------------------------------------------------------------------------


class TestSweep:
    """End-to-end sweeps over a realistic root."""

    def test_reports_the_job_dir_and_its_bytes(self, tmp_path, monkeypatch):
        """The candidate names ``job/`` and accounts for its size."""
        make_workspace(tmp_path, job_id="1", age_hours=500.0, core_bytes=8192)
        agent = make_agent(tmp_path, monkeypatch)
        report = agent.sweep(now=NOW)
        candidate = report.candidates[0]
        assert candidate["path"].endswith(f"job-1/{JOB_SUBDIR}")
        assert candidate["mode"] == "prune"
        assert candidate["bytes"] >= 8192
        assert report.reclaimable_bytes == candidate["bytes"]

    def test_counts_workspaces_and_usage(self, tmp_path, monkeypatch):
        """Usage covers every workspace, reclaimable only the candidates."""
        make_workspace(tmp_path, job_id="1", age_hours=500.0, core_bytes=4096)
        make_workspace(tmp_path, job_id="2", state="downloading", age_hours=500.0, core_bytes=4096)
        agent = make_agent(tmp_path, monkeypatch)
        report = agent.sweep(now=NOW)
        assert report.workspaces_scanned == 2
        assert report.usage_bytes > report.reclaimable_bytes

    def test_unmanaged_entries_are_reported_not_targeted(self, tmp_path, monkeypatch):
        """Stray files at the root are counted and left alone."""
        make_workspace(tmp_path, job_id="1", age_hours=500.0)
        (tmp_path / "probe.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        (tmp_path / "scratch").mkdir()
        agent = make_agent(tmp_path, monkeypatch)
        report = agent.sweep(now=NOW)
        names = sorted(Path(entry["path"]).name for entry in report.unmanaged)
        assert names == ["probe.sh", "scratch"]
        assert all("probe.sh" not in c["path"] for c in report.candidates)

    def test_lock_file_is_not_listed_as_unmanaged(self, tmp_path, monkeypatch):
        """``.busy.lock`` belongs to Bamboo MCP and is not our business."""
        make_workspace(tmp_path, job_id="1", age_hours=500.0)
        write_lock(tmp_path, {})
        agent = make_agent(tmp_path, monkeypatch)
        report = agent.sweep(now=NOW)
        assert report.unmanaged == []

    def test_missing_root_is_not_an_error(self, tmp_path, monkeypatch):
        """A root that does not exist yields an empty report, not a crash."""
        missing = tmp_path / "nope"
        monkeypatch.setattr(reaper, "ALLOWED_PREFIXES", (str(tmp_path),))
        agent = CoreReaperAgent(config=make_config(missing))
        agent.start()
        report = agent.sweep(now=NOW)
        assert report.workspaces_scanned == 0
        assert report.candidates == []

    def test_repeated_sweeps_are_identical(self, tmp_path, monkeypatch):
        """Re-running is harmless and produces the same answer."""
        make_workspace(tmp_path, job_id="1", age_hours=500.0)
        agent = make_agent(tmp_path, monkeypatch)
        first = agent.sweep(now=NOW).to_dict()
        second = agent.sweep(now=NOW).to_dict()
        first.pop("started_utc")
        second.pop("started_utc")
        assert first == second

    def test_symlinked_workspace_is_not_scanned(self, tmp_path, monkeypatch):
        """A symlinked ``job-*`` entry is never treated as a workspace."""
        real = tmp_path / "real"
        real.mkdir()
        (tmp_path / "job-9").symlink_to(real, target_is_directory=True)
        agent = make_agent(tmp_path, monkeypatch)
        report = agent.sweep(now=NOW)
        assert report.workspaces_scanned == 0

    def test_health_details_after_a_sweep(self, tmp_path, monkeypatch):
        """Health carries the sweep counters for the supervisor."""
        make_workspace(tmp_path, job_id="1", age_hours=500.0)
        agent = make_agent(tmp_path, monkeypatch)
        agent.tick()
        details = agent.health().details
        assert details["swept"] is True
        assert details["report_only"] is True
        assert details["candidates"] == 1

    def test_health_details_before_a_sweep(self, tmp_path, monkeypatch):
        """Health is meaningful before the first tick."""
        agent = make_agent(tmp_path, monkeypatch)
        assert agent.health().details == {"swept": False, "report_only": True}

    def test_lifecycle(self, tmp_path, monkeypatch):
        """The standard start/tick/stop lifecycle holds."""
        agent = make_agent(tmp_path, monkeypatch)
        assert agent.state is AgentState.RUNNING
        agent.tick()
        agent.stop()
        assert agent.state is AgentState.STOPPED

    def test_config_is_required(self):
        """Constructing without a config is a programming error."""
        with pytest.raises(ValueError):
            CoreReaperAgent()

    def test_start_does_not_create_the_root(self, tmp_path, monkeypatch):
        """Unlike the document monitor, this script never creates directories."""
        missing = tmp_path / "absent"
        monkeypatch.setattr(reaper, "ALLOWED_PREFIXES", (str(tmp_path),))
        agent = CoreReaperAgent(config=make_config(missing))
        agent.start()
        assert not missing.exists()


class TestPressurePass:
    """Pressure relaxes the age rule and nothing else."""

    def test_no_pressure_below_the_threshold(self, tmp_path, monkeypatch):
        """A young workspace stays untouched when there is room."""
        make_workspace(tmp_path, job_id="1", age_hours=0.75, core_bytes=1024)
        agent = make_agent(tmp_path, monkeypatch, quota_bytes=10_000_000)
        report = agent.sweep(now=NOW)
        assert report.candidates == []

    def test_pressure_reports_young_workspaces(self, tmp_path, monkeypatch):
        """Above the pressure mark, the age rule is relaxed to the floor."""
        make_workspace(tmp_path, job_id="1", age_hours=0.9, core_bytes=200_000)
        agent = make_agent(tmp_path, monkeypatch, quota_bytes=100_000)
        report = agent.sweep(now=NOW)
        assert len(report.candidates) == 1
        assert report.candidates[0]["pressure"] is True

    def test_pressure_respects_the_age_floor(self, tmp_path, monkeypatch):
        """An analysis that finished moments ago is never reached."""
        make_workspace(tmp_path, job_id="1", age_hours=0.1, core_bytes=200_000)
        agent = make_agent(tmp_path, monkeypatch, quota_bytes=100_000)
        report = agent.sweep(now=NOW)
        assert report.candidates == []

    def test_pressure_never_relaxes_the_other_rules(self, tmp_path, monkeypatch):
        """State, liveness and slot ownership hold under pressure."""
        make_workspace(tmp_path, job_id="1", state="downloading", age_hours=0.9, core_bytes=200_000)
        make_workspace(tmp_path, job_id="2", state="complete", age_hours=0.9, worker_pid=os.getpid(), core_bytes=200_000)
        make_workspace(tmp_path, job_id="3", state="complete", age_hours=0.9, core_bytes=200_000)
        write_lock(tmp_path, {"job_id": "3"})
        agent = make_agent(tmp_path, monkeypatch, quota_bytes=100_000)
        report = agent.sweep(now=NOW)
        assert report.candidates == []

    def test_pressure_takes_the_oldest_first(self, tmp_path, monkeypatch):
        """Oldest-first ordering, stopping once the target is projected met."""
        make_workspace(tmp_path, job_id="1", age_hours=0.6, core_bytes=100_000)
        make_workspace(tmp_path, job_id="2", age_hours=0.95, core_bytes=100_000)
        make_workspace(tmp_path, job_id="3", age_hours=0.8, core_bytes=100_000)
        agent = make_agent(tmp_path, monkeypatch, quota_bytes=350_000, pressure_pct=80.0, target_pct=60.0)
        report = agent.sweep(now=NOW)
        assert [c["job_id"] for c in report.candidates] == ["2"]

    def test_pressure_is_disabled_by_a_zero_quota(self, tmp_path, monkeypatch):
        """A non-positive quota disables the pressure pass entirely."""
        make_workspace(tmp_path, job_id="1", age_hours=0.9, core_bytes=200_000)
        agent = make_agent(tmp_path, monkeypatch, quota_bytes=0)
        report = agent.sweep(now=NOW)
        assert report.candidates == []


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


class TestUtilities:
    """Sizing, PID liveness and environment resolution."""

    def test_directory_usage_skips_symlinks(self, tmp_path):
        """Symlinks contribute nothing, matching Bamboo MCP's walker."""
        (tmp_path / "real.bin").write_bytes(b"\0" * 100)
        (tmp_path / "link.bin").symlink_to(tmp_path / "real.bin")
        assert directory_usage_bytes(tmp_path) == 100

    def test_directory_usage_of_a_missing_dir(self, tmp_path):
        """A missing directory measures zero."""
        assert directory_usage_bytes(tmp_path / "nope") == 0

    def test_directory_usage_survives_unreadable_entries(self, tmp_path, monkeypatch):
        """One unreadable file must not abort the walk."""
        (tmp_path / "a.bin").write_bytes(b"\0" * 10)
        (tmp_path / "b.bin").write_bytes(b"\0" * 10)
        real_stat = Path.stat

        def _flaky(self, *args, **kwargs):
            if self.name == "a.bin":
                raise OSError("permission denied")
            return real_stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", _flaky)
        assert directory_usage_bytes(tmp_path) == 10

    def test_pid_liveness(self):
        """The current process is alive; a huge PID is not."""
        assert is_pid_alive(os.getpid()) is True
        assert is_pid_alive(DEAD_PID) is False

    @pytest.mark.parametrize("pid", [None, 0, -1])
    def test_pid_liveness_edge_cases(self, pid):
        """Null and non-positive PIDs are not alive."""
        assert is_pid_alive(pid) is False

    def test_resolve_root_prefers_the_explicit_value(self, monkeypatch):
        """CLI value beats the environment."""
        monkeypatch.setenv(reaper.ENV_ROOT, "/tmp/bamboo/from-env")
        assert resolve_root("/tmp/bamboo/explicit") == Path("/tmp/bamboo/explicit")

    def test_resolve_root_falls_back_to_the_environment(self, monkeypatch):
        """The environment variable is read rather than the default hardcoded."""
        monkeypatch.setenv(reaper.ENV_ROOT, "/tmp/bamboo/from-env")
        assert resolve_root(None) == Path("/tmp/bamboo/from-env")

    def test_resolve_root_default(self, monkeypatch):
        """With nothing set, the documented default applies."""
        monkeypatch.delenv(reaper.ENV_ROOT, raising=False)
        assert resolve_root(None) == Path(reaper.DEFAULT_ROOT)

    def test_resolve_quota_from_environment(self, monkeypatch):
        """The same variable Bamboo MCP's check_quota() uses."""
        monkeypatch.setenv(reaper.ENV_QUOTA, "12345")
        assert resolve_quota_bytes(None) == 12345

    def test_resolve_quota_ignores_garbage(self, monkeypatch):
        """A non-integer setting falls back to the default with a warning."""
        monkeypatch.setenv(reaper.ENV_QUOTA, "lots")
        assert resolve_quota_bytes(None) == reaper.DEFAULT_QUOTA_BYTES

    @pytest.mark.parametrize(
        "size,expected",
        [(0, "0 B"), (1024, "1.0 KiB"), (1024 ** 3, "1.0 GiB")],
    )
    def test_human_bytes(self, size, expected):
        """Byte counts format readably for the log."""
        assert human_bytes(size) == expected


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCli:
    """Argument handling and exit codes."""

    def test_parser_defaults(self):
        """Nothing is applied by default because nothing can be."""
        args = build_parser().parse_args([])
        assert args.once is False
        assert args.format == "text"
        assert not hasattr(args, "apply")

    def test_no_apply_flag_exists(self):
        """There is deliberately no way to ask for deletion."""
        with pytest.raises(SystemExit):
            build_parser().parse_args(["--apply"])

    def test_once_run_reports_and_exits_zero(self, tmp_path, monkeypatch, capsys):
        """A clean sweep below the pressure mark exits 0."""
        root = tmp_path / "core-analysis"
        root.mkdir()
        make_workspace(root, job_id="1", age_hours=500.0, core_bytes=1024)
        monkeypatch.setattr(reaper, "ALLOWED_PREFIXES", (str(root),))
        code = main([
            "--config", str(tmp_path / "absent.yaml"),
            "--root", str(root),
            "--quota-bytes", "100000000",
            "--log-file", os.devnull,
            "--once",
        ])
        out = capsys.readouterr().out
        assert code == EXIT_OK
        assert "could have removed" in out
        assert "Nothing was removed" in out

    def test_pressure_exit_code(self, tmp_path, monkeypatch, capsys):
        """Usage above the pressure mark with reclaimable space exits 3."""
        root = tmp_path / "core-analysis"
        root.mkdir()
        make_workspace(root, job_id="1", age_hours=500.0, core_bytes=200_000)
        monkeypatch.setattr(reaper, "ALLOWED_PREFIXES", (str(root),))
        code = main([
            "--config", str(tmp_path / "absent.yaml"),
            "--root", str(root),
            "--quota-bytes", "100000",
            "--log-file", os.devnull,
            "--once",
        ])
        capsys.readouterr()
        assert code == EXIT_PRESSURE

    def test_json_output(self, tmp_path, monkeypatch, capsys):
        """``--format json`` emits a parseable report."""
        root = tmp_path / "core-analysis"
        root.mkdir()
        make_workspace(root, job_id="1", age_hours=500.0)
        monkeypatch.setattr(reaper, "ALLOWED_PREFIXES", (str(root),))
        main([
            "--config", str(tmp_path / "absent.yaml"),
            "--root", str(root),
            "--quota-bytes", "100000000",
            "--format", "json",
            "--log-file", os.devnull,
            "--once",
        ])
        payload = json.loads(capsys.readouterr().out)
        assert payload["report_only"] is True
        assert payload["removed_bytes"] == 0
        assert payload["candidates"][0]["job_id"] == "1"

    def test_unreadable_config_is_a_usage_error(self, tmp_path):
        """A malformed config file exits 2 rather than sweeping blind."""
        bad = tmp_path / "bad.yaml"
        bad.write_text("just a string", encoding="utf-8")
        assert main(["--config", str(bad), "--log-file", os.devnull, "--once"]) == EXIT_USAGE

    def test_config_file_values_are_used(self, tmp_path, monkeypatch, capsys):
        """Settings come from the config file when no flag overrides them."""
        root = tmp_path / "core-analysis"
        root.mkdir()
        make_workspace(root, job_id="1", age_hours=5.0)
        cfg = tmp_path / "reaper.yaml"
        cfg.write_text(f"root: {root}\nmin_age_hours: 100.0\nquota_bytes: 100000000\n", encoding="utf-8")
        monkeypatch.setattr(reaper, "ALLOWED_PREFIXES", (str(root),))
        code = main(["--config", str(cfg), "--log-file", os.devnull, "--once"])
        out = capsys.readouterr().out
        assert code == EXIT_OK
        assert "could have removed" not in out

    def test_cli_overrides_the_config_file(self, tmp_path, monkeypatch, capsys):
        """A flag beats the same setting in the config file."""
        root = tmp_path / "core-analysis"
        root.mkdir()
        make_workspace(root, job_id="1", age_hours=5.0)
        cfg = tmp_path / "reaper.yaml"
        cfg.write_text(f"root: {root}\nmin_age_hours: 100.0\nquota_bytes: 100000000\n", encoding="utf-8")
        monkeypatch.setattr(reaper, "ALLOWED_PREFIXES", (str(root),))
        main(["--config", str(cfg), "--min-age-hours", "1.0", "--log-file", os.devnull, "--once"])
        assert "could have removed" in capsys.readouterr().out

    def test_sweep_errors_exit_one(self, tmp_path, monkeypatch, capsys):
        """A root that cannot be listed is a non-fatal error, exit 1."""
        root = tmp_path / "core-analysis"
        root.mkdir()
        monkeypatch.setattr(reaper, "ALLOWED_PREFIXES", (str(root),))

        def _boom(self):
            raise OSError("permission denied")

        monkeypatch.setattr(Path, "iterdir", _boom)
        code = main([
            "--config", str(tmp_path / "absent.yaml"),
            "--root", str(root),
            "--log-file", os.devnull,
            "--once",
        ])
        capsys.readouterr()
        assert code == EXIT_ERRORS
