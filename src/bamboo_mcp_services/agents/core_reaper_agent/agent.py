"""Core-dump workspace reaper — reports reclaimable space, removes nothing.

``atlas.core_dump_analysis`` in Bamboo MCP downloads PanDA job core dumps —
routinely 1 GB each — into ``$BAMBOO_CORE_ANALYSIS_ROOT`` and never deletes
anything.  That is a deliberate rule in *that* codebase.  Reaping is a separate
concern and lives here.

**This version removes nothing.**  It walks the analysis root, decides which
workspaces *would* be safe to reclaim, and logs a line per candidate::

    I could have removed: /tmp/bamboo/core-analysis/job-7272161793/job

There is no ``--apply`` flag, and this module contains no destructive call:
no ``shutil.rmtree``, no ``os.remove``/``os.unlink``/``os.rmdir``, no
``Path.unlink``/``Path.rmdir``, no ``subprocess``.  Every file it touches is
opened read-only.  The invariant is enforced by
``test_core_reaper_agent.py::TestNoDeletionInvariant``, which scans this
module's own source for those names — do not delete that test, and do not
introduce a destructive call without first deciding, deliberately, that the
report-only contract is over.

The path guard (:func:`assert_reclaimable_path`) is nevertheless fully
implemented and every reported candidate must pass it.  That way the guard is
exercised against real production layouts long before any deletion code
exists, and a future ``--apply`` has exactly one choke point to call.

Safety rules for reclaiming a workspace — all four must hold:

1. ``state`` in the manifest is ``complete`` or ``failed``.
2. ``finished_utc`` (falling back to ``updated_utc``) is older than the
   retention age.
3. The workspace does not currently hold the ``.busy.lock`` slot.
4. ``worker_pid`` is ``null`` or not a live process.

Rules 1 and 4 are checked independently: a manifest can be stale, and Bamboo
MCP's own ``reconcile_state`` may still want a workspace whose worker died
mid-download.  Worker liveness is tested by looking for ``/proc/<pid>``; no
signal is ever sent, so Bamboo MCP's "leave processes alone" rule for this
directory holds by construction rather than by argument.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from bamboo_mcp_services.agents.base import Agent

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants mirrored from Bamboo MCP's core_dump_analysis module.
# These are intentionally duplicated rather than imported: the two packages
# are kept fully independent, with no cross-package imports in either
# direction.  If they ever drift, this module refuses to act rather than
# guessing (see SUPPORTED_MANIFEST_VERSION).
# ---------------------------------------------------------------------------

#: Environment variable naming the analysis root directory.
ENV_ROOT = "BAMBOO_CORE_ANALYSIS_ROOT"

#: Default analysis root when ENV_ROOT is unset.
DEFAULT_ROOT = "/tmp/bamboo/core-analysis"

#: Environment variable naming the byte quota used by Bamboo MCP's check_quota().
ENV_QUOTA = "BAMBOO_CORE_ANALYSIS_QUOTA_BYTES"

#: Default quota (50 GiB), matching Bamboo MCP's default.
DEFAULT_QUOTA_BYTES = 50 * 1024 * 1024 * 1024

#: Per-workspace manifest filename — the state store.
MANIFEST_NAME = ".bamboo-core-analysis.json"

#: Single-slot lock filename at the analysis root.
LOCK_NAME = ".busy.lock"

#: Subdirectory holding the reconstructed PanDA job directory (the big one).
JOB_SUBDIR = "job"

#: Small files preserved by prune mode — the entire diagnostic record of a run.
KEEP_ON_PRUNE = ("evidence.json", "gdb_raw.txt", "worker.log")

#: Manifest schema version this code understands.  Anything else is refused.
SUPPORTED_MANIFEST_VERSION = 1

#: Manifest states meaning the run is over.
TERMINAL_STATES = frozenset({"complete", "failed"})

#: Workspace directory names are exactly ``job-<digits>``.
WORKSPACE_RE = re.compile(r"^job-\d+$")

#: Filenames that may never be a reclaim target under any circumstances.
RESERVED_NAMES = frozenset({LOCK_NAME, MANIFEST_NAME, *KEEP_ON_PRUNE})

#: Absolute path prefixes under which reclaiming is permitted at all.
#:
#: Hardcoded on purpose.  There is no CLI flag and no environment variable to
#: widen this list: a deployment that moves the analysis root outside these
#: prefixes gets a refusal and a log line, not a reclaim.  Changing it is a
#: code change with a code review.  (:func:`assert_reclaimable_path` takes the
#: list as a defaulted parameter so unit tests can drive it against pytest's
#: ``tmp_path``; nothing at runtime ever passes that argument.)
ALLOWED_PREFIXES: Tuple[str, ...] = ("/tmp/bamboo",)

#: Paths that are never a valid target, regardless of any other check.
FORBIDDEN_TARGETS = frozenset({"/", "/tmp", "/tmp/bamboo", "/data", "/data/bamboo"})


class UnsafePathError(Exception):
    """Raised when a candidate path fails the reclaim path guard."""


class Action(str, Enum):
    """What the reaper would do with a workspace.

    Attributes:
        SKIP: Nothing to do; ``Decision.reason`` explains why.
        PRUNE_JOB_DIR: Would remove ``<workspace>/job`` only.
        REMOVE_WORKSPACE: Would remove the whole workspace directory.
    """

    SKIP = "skip"
    PRUNE_JOB_DIR = "prune"
    REMOVE_WORKSPACE = "purge"


@dataclass(frozen=True)
class Decision:
    """Outcome of classifying a single workspace.

    Attributes:
        action: What would happen to this workspace.
        reason: Short machine-readable reason code, e.g. ``'terminal-and-aged'``
            or ``'worker-alive'``.
        target: Path that would be removed, or ``None`` for ``SKIP``.
        bytes_estimate: Bytes that would be reclaimed by *action*.
        age_hours: Age of the workspace in hours, or ``None`` when unknown.
        young_only: True when the only failed rule was the retention age, which
            makes this workspace eligible for the quota-pressure pass.
    """

    action: Action
    reason: str
    target: Optional[Path] = None
    bytes_estimate: int = 0
    age_hours: Optional[float] = None
    young_only: bool = False


@dataclass(frozen=True)
class Manifest:
    """Parsed ``.bamboo-core-analysis.json``.

    Attributes:
        readable: False when the file was missing, unparseable, or carried an
            unknown ``manifest_version``.  ``problem`` says which.
        problem: Reason code when *readable* is False, otherwise ``None``.
        manifest_version: Schema version as found on disk.
        job_id: PanDA job ID.
        request_id: Identifier of the run that produced this workspace.
        state: One of queued/preparing/downloading/analyzing/complete/failed.
        created_utc: Creation timestamp, if present.
        updated_utc: Last-update timestamp, if present.
        finished_utc: Completion timestamp, if present.
        worker_pid: PID of the detached worker, or ``None``.
        bytes_downloaded: Progress counter.
        error: Failure message, or ``None``.
    """

    readable: bool
    problem: Optional[str] = None
    manifest_version: Optional[int] = None
    job_id: Optional[str] = None
    request_id: Optional[str] = None
    state: Optional[str] = None
    created_utc: Optional[datetime] = None
    updated_utc: Optional[datetime] = None
    finished_utc: Optional[datetime] = None
    worker_pid: Optional[int] = None
    bytes_downloaded: Optional[int] = None
    error: Optional[str] = None

    @property
    def reference_time(self) -> Optional[datetime]:
        """Return the timestamp used for age comparisons.

        Returns:
            ``finished_utc`` when present, otherwise ``updated_utc``, otherwise
            ``None``.
        """
        return self.finished_utc or self.updated_utc

    @classmethod
    def load(cls, path: Path) -> "Manifest":
        """Read and parse a manifest file.

        The file is opened read-only and never written back.  Any problem —
        absence, unreadable bytes, malformed JSON, a non-object payload, or a
        ``manifest_version`` this code does not know — yields an unreadable
        manifest with a reason code rather than an exception, so one bad
        workspace cannot abort a sweep.

        Args:
            path: Path to ``.bamboo-core-analysis.json``.

        Returns:
            A :class:`Manifest`.  Check ``readable`` before using any field.
        """
        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except FileNotFoundError:
            return cls(readable=False, problem="no-manifest")
        except (OSError, ValueError) as exc:
            logger.warning("Unreadable manifest %s: %s", path, exc)
            return cls(readable=False, problem="manifest-unparseable")

        if not isinstance(raw, dict):
            logger.warning("Manifest %s is not a JSON object.", path)
            return cls(readable=False, problem="manifest-unparseable")

        version = raw.get("manifest_version")
        if version != SUPPORTED_MANIFEST_VERSION:
            logger.warning(
                "Manifest %s has unsupported manifest_version=%r (expected %d) — refusing to act on it.",
                path,
                version,
                SUPPORTED_MANIFEST_VERSION,
            )
            return cls(readable=False, problem="manifest-version-unsupported", manifest_version=_as_int(version))

        return cls(
            readable=True,
            manifest_version=SUPPORTED_MANIFEST_VERSION,
            job_id=_as_str(raw.get("job_id")),
            request_id=_as_str(raw.get("request_id")),
            state=_as_str(raw.get("state")),
            created_utc=parse_iso_utc(raw.get("created_utc")),
            updated_utc=parse_iso_utc(raw.get("updated_utc")),
            finished_utc=parse_iso_utc(raw.get("finished_utc")),
            worker_pid=_as_int(raw.get("worker_pid")),
            bytes_downloaded=_as_int(raw.get("bytes_downloaded")),
            error=_as_str(raw.get("error")),
        )


@dataclass
class Workspace:
    """A single ``job-<id>`` directory under the analysis root.

    Attributes:
        path: Absolute path to the workspace directory.
        job_id: Job ID taken from the directory name.
        manifest: Parsed manifest (possibly unreadable).
        total_bytes: Bytes used by the whole workspace.
        job_dir_bytes: Bytes used by ``<workspace>/job``.
        newest_mtime: Most recent mtime found anywhere in the workspace, used
            as the age proxy when there is no manifest.
        has_job_dir: Whether ``<workspace>/job`` exists as a real directory.
        job_dir_is_symlink: Whether ``<workspace>/job`` is a symlink.  Reported
            separately so it is never quietly mistaken for an already-pruned
            workspace: ``job/`` is reconstructed from a remote file listing, so
            a symlink there is exactly the case the path guard exists for.
    """

    path: Path
    job_id: str
    manifest: Manifest
    total_bytes: int
    job_dir_bytes: int
    newest_mtime: Optional[datetime]
    has_job_dir: bool
    job_dir_is_symlink: bool = False


@dataclass
class CoreReaperConfig:
    """Configuration for :class:`CoreReaperAgent`.

    Attributes:
        root: Analysis root to sweep.
        min_age_hours: Minimum age, in hours, before a terminal workspace is
            reported as reclaimable.  Defaults to 1.0 — core dumps are large
            and a busy node fills up fast, so the reaper is deliberately
            aggressive about *reporting*; it still removes nothing.
        orphan_age_hours: Longer threshold applied to workspaces that have no
            manifest at all, since those are ambiguous (possibly a run that
            crashed between mkdir and the first manifest write).
        mode: ``'prune'`` (report ``<workspace>/job`` only, the default) or
            ``'purge'`` (report the whole workspace).
        purge_failed_after_hours: In prune mode, escalate to whole-workspace
            removal for ``failed`` runs older than this many hours.  ``None``
            disables the escalation.
        quota_bytes: Byte ceiling used for pressure calculations; mirrors
            ``BAMBOO_CORE_ANALYSIS_QUOTA_BYTES``.
        pressure_pct: Usage percentage of *quota_bytes* at which the pressure
            pass engages.
        target_pct: Usage percentage the pressure pass tries to get below.
        min_age_floor_hours: Hard floor the pressure pass may never go below,
            so pressure cannot reach an analysis that finished moments ago.
        tick_interval_s: Seconds between sweeps in daemon mode.
    """

    root: Path
    min_age_hours: float = 1.0
    orphan_age_hours: float = 24.0
    mode: str = Action.PRUNE_JOB_DIR.value
    purge_failed_after_hours: Optional[float] = None
    quota_bytes: int = DEFAULT_QUOTA_BYTES
    pressure_pct: float = 80.0
    target_pct: float = 60.0
    min_age_floor_hours: float = 0.5
    tick_interval_s: float = 3600.0


@dataclass
class SweepReport:
    """Result of one sweep.

    Attributes:
        root: Root that was swept.
        started_utc: Sweep start time.
        usage_bytes: Total bytes under the root.
        quota_bytes: Configured quota.
        reclaimable_bytes: Bytes the reaper would have reclaimed.
        workspaces_scanned: Number of ``job-*`` directories examined.
        candidates: One entry per reportable candidate.
        skipped: Reason code to count, for workspaces left alone.
        refused: Candidates rejected by the path guard, with the guard reason.
        unmanaged: Entries under the root that are not ``job-*`` workspaces.
        errors: Non-fatal errors encountered during the sweep.
    """

    root: str
    started_utc: datetime
    usage_bytes: int = 0
    quota_bytes: int = DEFAULT_QUOTA_BYTES
    reclaimable_bytes: int = 0
    workspaces_scanned: int = 0
    candidates: List[Dict[str, Any]] = field(default_factory=list)
    skipped: Dict[str, int] = field(default_factory=dict)
    refused: List[Dict[str, Any]] = field(default_factory=list)
    unmanaged: List[Dict[str, Any]] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    @property
    def usage_pct(self) -> float:
        """Return usage as a percentage of the quota.

        Returns:
            Percentage, or 0.0 when the quota is not positive.
        """
        if self.quota_bytes <= 0:
            return 0.0
        return 100.0 * self.usage_bytes / self.quota_bytes

    def to_dict(self) -> Dict[str, Any]:
        """Convert the report to a JSON-serialisable dictionary.

        Returns:
            Dictionary with ISO-8601 timestamps and plain-string paths.
        """
        return {
            "root": self.root,
            "started_utc": self.started_utc.astimezone(timezone.utc).isoformat(),
            "usage_bytes": self.usage_bytes,
            "quota_bytes": self.quota_bytes,
            "usage_pct": round(self.usage_pct, 2),
            "reclaimable_bytes": self.reclaimable_bytes,
            "workspaces_scanned": self.workspaces_scanned,
            "candidates": self.candidates,
            "skipped": dict(self.skipped),
            "refused": self.refused,
            "unmanaged": self.unmanaged,
            "errors": list(self.errors),
            "removed_bytes": 0,
            "report_only": True,
        }


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _as_str(value: Any) -> Optional[str]:
    """Coerce a JSON value to ``str``.

    Args:
        value: Value from the manifest.

    Returns:
        The value as a string, or ``None`` when it was ``None``.
    """
    return None if value is None else str(value)


def _as_int(value: Any) -> Optional[int]:
    """Coerce a JSON value to ``int``.

    Args:
        value: Value from the manifest.

    Returns:
        The value as an int, or ``None`` when absent or non-numeric.
    """
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def parse_iso_utc(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp into an aware UTC datetime.

    Args:
        value: String timestamp, possibly ending in ``Z``, or ``None``.

    Returns:
        Timezone-aware UTC datetime, or ``None`` when the value is absent or
        cannot be parsed.  A naive timestamp is assumed to be UTC.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def is_pid_alive(pid: Optional[int]) -> bool:
    """Return whether *pid* refers to a live process.

    Liveness is determined by the existence of ``/proc/<pid>``.  No signal is
    sent — not even signal 0 — because Bamboo MCP's rule for this directory is
    that workers are never signalled.  On systems without ``/proc`` the check
    falls back to ``os.kill(pid, 0)``, which sends no signal either but does
    consult process permissions.

    PID reuse can only make this return True for a worker that is actually
    gone, which makes the reaper more conservative, never less.

    Args:
        pid: Process ID, or ``None``.

    Returns:
        True when the process appears to exist.  ``None`` and non-positive
        PIDs return False.
    """
    if pid is None or pid <= 0:
        return False
    proc_root = Path("/proc")
    if proc_root.is_dir():
        return (proc_root / str(pid)).exists()
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def directory_usage_bytes(path: Path) -> int:
    """Sum the sizes of regular files under *path*.

    Mirrors the shape of Bamboo MCP's ``workspace_usage_bytes()``: recursive
    walk, symlinks skipped, unreadable entries ignored.  The figure is for a
    threshold, not accounting — one unreadable file must never abort a sweep.

    ``st_size`` is used rather than ``st_blocks`` so the arithmetic agrees
    with Bamboo MCP's ``check_quota()``, at the cost of overstating sparse
    core files.

    Args:
        path: Directory to measure.  A missing directory yields 0.

    Returns:
        Total size in bytes.
    """
    total = 0
    if not path.is_dir():
        return 0
    for entry in path.rglob("*"):
        try:
            if entry.is_symlink() or not entry.is_file():
                continue
            total += entry.stat().st_size
        except OSError:
            continue
    return total


def newest_mtime(path: Path) -> Optional[datetime]:
    """Return the most recent mtime found at or below *path*.

    Used as the age proxy for workspaces that carry no manifest.

    Args:
        path: Directory to scan.

    Returns:
        Aware UTC datetime, or ``None`` when nothing could be stat'ed.
    """
    newest: Optional[float] = None
    try:
        newest = path.lstat().st_mtime
    except OSError:
        newest = None
    for entry in path.rglob("*"):
        try:
            stamp = entry.lstat().st_mtime
        except OSError:
            continue
        if newest is None or stamp > newest:
            newest = stamp
    if newest is None:
        return None
    return datetime.fromtimestamp(newest, tz=timezone.utc)


def read_slot_holder(root: Path) -> Optional[str]:
    """Return the job ID currently holding the single analysis slot.

    Two separate things live in ``.busy.lock`` and conflating them causes
    bugs.  The ``fcntl.flock`` guards only the read-modify-write of the file
    and is held for microseconds; the *content* is the ownership record and
    outlives both the flock and the process that wrote it.  Holding the flock
    therefore does not mean holding the slot, and a free flock does not mean
    no analysis is running.  This function takes the flock only for the
    duration of the read, then reports on the content.

    The file is opened read-only and is never created, rewritten or removed.

    Args:
        root: Analysis root directory.

    Returns:
        Job ID of the current holder, or ``None`` when the slot is free, the
        lock file is absent, or its content is unreadable.
    """
    lock_path = root / LOCK_NAME
    try:
        fd = os.open(str(lock_path), os.O_RDONLY)
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("Cannot open %s: %s — assuming no slot holder.", lock_path, exc)
        return None

    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError as exc:
            logger.warning("Cannot flock %s: %s — reading content anyway.", lock_path, exc)
        try:
            payload = os.read(fd, 65536).decode("utf-8", errors="replace")
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        os.close(fd)

    text = payload.strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        logger.warning("Content of %s is not JSON — assuming no slot holder.", lock_path)
        return None
    if not isinstance(data, dict) or not data:
        return None
    return _as_str(data.get("job_id"))


# ---------------------------------------------------------------------------
# The path guard
# ---------------------------------------------------------------------------


def _expand_prefixes(prefixes: Sequence[str]) -> Tuple[str, ...]:
    """Expand an allowlist to include the real path of each prefix.

    ``/tmp`` is a symlink to ``/private/tmp`` on macOS, so both the literal
    and resolved forms are accepted for the *prefix* itself.  Symlinks
    *inside* the root are rejected separately and unconditionally.

    Args:
        prefixes: Configured allowlist.

    Returns:
        Tuple of normalised absolute prefixes.
    """
    expanded = []
    for prefix in prefixes:
        literal = os.path.normpath(os.path.abspath(prefix))
        expanded.append(literal)
        real = os.path.realpath(prefix)
        if real != literal:
            expanded.append(real)
    return tuple(expanded)


def _is_under(path: str, parent: str) -> bool:
    """Return whether *path* is strictly below *parent*.

    Args:
        path: Normalised absolute path.
        parent: Normalised absolute directory path.

    Returns:
        True when *path* is a strict descendant of *parent*.
    """
    if path == parent:
        return False
    return path.startswith(parent.rstrip(os.sep) + os.sep)


def _check_no_symlink_below_root(path: Path, root: Path) -> None:
    """Verify no component between *root* and *path* is a symlink.

    ``job/`` is reconstructed from a remote file listing, so a symlinked
    component is a realistic way for a deletion to escape the root.

    Args:
        path: Candidate path.
        root: Analysis root.

    Raises:
        UnsafePathError: If any component below the root is a symlink.
    """
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise UnsafePathError(f"not-under-root: {path}") from exc
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise UnsafePathError(f"symlink-in-path: {current}")


def _check_shape(path: Path, kind: str) -> None:
    """Verify the candidate has the expected name shape.

    Args:
        path: Candidate path.
        kind: ``'workspace'`` or ``'job_dir'``.

    Raises:
        UnsafePathError: If the name does not match the expected shape or the
            basename is a reserved filename.
    """
    if path.name in RESERVED_NAMES:
        raise UnsafePathError(f"reserved-name: {path}")
    if kind == "workspace":
        if not WORKSPACE_RE.match(path.name):
            raise UnsafePathError(f"not-a-workspace-name: {path}")
    elif kind == "job_dir":
        if path.name != JOB_SUBDIR or not WORKSPACE_RE.match(path.parent.name):
            raise UnsafePathError(f"not-a-job-dir: {path}")
    else:
        raise UnsafePathError(f"unknown-target-kind: {kind}")


def _check_same_filesystem(path: Path, root: Path) -> None:
    """Verify *path* sits on the same filesystem as *root*.

    Catches a bind mount or mount point underneath the root that a plain
    prefix check would happily accept.

    Args:
        path: Candidate path.
        root: Analysis root.

    Raises:
        UnsafePathError: If the device IDs differ or either cannot be stat'ed.
    """
    try:
        if os.lstat(str(path)).st_dev != os.stat(str(root)).st_dev:
            raise UnsafePathError(f"cross-filesystem: {path}")
    except OSError as exc:
        raise UnsafePathError(f"unstatable: {path} ({exc})") from exc


def assert_reclaimable_path(
    path: Path,
    root: Path,
    kind: str,
    allowed_prefixes: Optional[Sequence[str]] = None,
) -> None:
    """Raise unless *path* is safe to reclaim.

    This is the single choke point for reclaim safety.  Nothing in this
    version deletes, but every reported candidate passes through here so the
    guard is exercised against real layouts, and a future ``--apply`` has
    exactly one function to call immediately before each unlink.

    Seven independent checks, in order: hardcoded prefix allowlist (both the
    literal and the resolved form of the path); strict containment under the
    configured root; no symlink in any component below the root; not a
    forbidden or implausibly shallow path; correct name shape for the target
    kind; same filesystem as the root; not a reserved filename.

    Args:
        path: Candidate path to reclaim.
        root: Configured analysis root.
        kind: ``'workspace'`` for a whole ``job-<id>`` directory, or
            ``'job_dir'`` for ``<workspace>/job``.
        allowed_prefixes: Absolute prefixes under which reclaiming is
            permitted.  ``None`` — the only value used at runtime — means the
            hardcoded :data:`ALLOWED_PREFIXES`, resolved at call time so the
            constant stays the single source of truth.  Only unit tests pass
            anything else.

    Raises:
        UnsafePathError: If any check fails.  The message begins with a short
            reason code.
    """
    literal = Path(os.path.normpath(os.path.abspath(str(path))))
    literal_root = Path(os.path.normpath(os.path.abspath(str(root))))
    resolved = Path(os.path.realpath(str(path)))
    resolved_root = Path(os.path.realpath(str(root)))

    prefixes = _expand_prefixes(ALLOWED_PREFIXES if allowed_prefixes is None else allowed_prefixes)
    for candidate in (str(literal), str(resolved)):
        if not any(_is_under(candidate, prefix) for prefix in prefixes):
            raise UnsafePathError(f"outside-allowlist: {candidate}")

    if not _is_under(str(literal), str(literal_root)):
        raise UnsafePathError(f"not-under-root: {literal}")
    if not _is_under(str(resolved), str(resolved_root)):
        raise UnsafePathError(f"escapes-root: {resolved}")

    _check_no_symlink_below_root(literal, literal_root)

    if str(literal) in FORBIDDEN_TARGETS or str(resolved) in FORBIDDEN_TARGETS:
        raise UnsafePathError(f"forbidden-target: {literal}")
    if len(resolved.parts) < 3:
        raise UnsafePathError(f"too-shallow: {resolved}")

    _check_shape(literal, kind)
    _check_same_filesystem(literal, literal_root)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def _blocking_reason(workspace: Workspace, holder_job_id: Optional[str]) -> Optional[str]:
    """Return the reason a workspace may never be reclaimed, if any.

    Covers every rule except the retention age, so the caller can distinguish
    "not yet old enough" (relaxable under quota pressure) from "not safe"
    (never relaxable).

    Args:
        workspace: Workspace under consideration.
        holder_job_id: Job ID currently holding the slot, or ``None``.

    Returns:
        A reason code, or ``None`` when only the age rule remains to check.
    """
    manifest = workspace.manifest
    if holder_job_id is not None and holder_job_id == workspace.job_id:
        return "holds-slot"
    if not manifest.readable:
        return manifest.problem or "manifest-unreadable"
    if manifest.state not in TERMINAL_STATES:
        return f"non-terminal-state:{manifest.state}"
    if is_pid_alive(manifest.worker_pid):
        return "worker-alive"
    if manifest.reference_time is None:
        return "no-timestamp"
    return None


def _target_for(workspace: Workspace, config: CoreReaperConfig, age_hours: float) -> Tuple[Action, Path, int]:
    """Choose the reclaim action and target for an eligible workspace.

    Prune is the default because ``job/`` holds essentially all of the bytes
    while the manifest, ``evidence.json``, ``gdb_raw.txt`` and ``worker.log``
    together are kilobytes and are the entire diagnostic record of the run.
    Whole-workspace removal is reserved for ``purge`` mode and for old
    ``failed`` runs when the escalation is configured.

    Args:
        workspace: Eligible workspace.
        config: Reaper configuration.
        age_hours: Age of the workspace in hours.

    Returns:
        Tuple of (action, target path, estimated bytes reclaimed).
    """
    escalate = (
        config.purge_failed_after_hours is not None
        and workspace.manifest.state == "failed"
        and age_hours >= config.purge_failed_after_hours
    )
    if config.mode == Action.REMOVE_WORKSPACE.value or escalate:
        return Action.REMOVE_WORKSPACE, workspace.path, workspace.total_bytes
    return Action.PRUNE_JOB_DIR, workspace.path / JOB_SUBDIR, workspace.job_dir_bytes


def classify(
    workspace: Workspace,
    holder_job_id: Optional[str],
    now: datetime,
    config: CoreReaperConfig,
    min_age_hours: Optional[float] = None,
) -> Decision:
    """Decide what would happen to a workspace.

    Pure function: it reads no files and mutates nothing, so the whole safety
    matrix is unit-testable without a filesystem.

    Args:
        workspace: Workspace to classify.
        holder_job_id: Job ID currently holding the slot, or ``None``.
        now: Current time, timezone-aware.
        config: Reaper configuration.
        min_age_hours: Override for the retention age, used by the pressure
            pass.  Defaults to ``config.min_age_hours``.

    Returns:
        A :class:`Decision`.
    """
    threshold = config.min_age_hours if min_age_hours is None else min_age_hours

    if workspace.manifest.problem == "no-manifest":
        return _classify_orphan(workspace, holder_job_id, now, config)

    blocker = _blocking_reason(workspace, holder_job_id)
    if blocker is not None:
        return Decision(action=Action.SKIP, reason=blocker)

    reference = workspace.manifest.reference_time
    age_hours = (now - reference).total_seconds() / 3600.0
    if age_hours < threshold:
        return Decision(action=Action.SKIP, reason="too-young", age_hours=age_hours, young_only=True)

    action, target, size = _target_for(workspace, config, age_hours)
    if action == Action.PRUNE_JOB_DIR and workspace.job_dir_is_symlink:
        return Decision(action=Action.SKIP, reason="job-dir-symlink", age_hours=age_hours)
    if action == Action.PRUNE_JOB_DIR and not workspace.has_job_dir:
        return Decision(action=Action.SKIP, reason="already-pruned", age_hours=age_hours)
    return Decision(
        action=action,
        reason=f"terminal-and-aged:{workspace.manifest.state}",
        target=target,
        bytes_estimate=size,
        age_hours=age_hours,
    )


def _classify_orphan(
    workspace: Workspace,
    holder_job_id: Optional[str],
    now: datetime,
    config: CoreReaperConfig,
) -> Decision:
    """Classify a workspace that has no manifest at all.

    A missing manifest is ambiguous — it may be a run that crashed between
    ``mkdir`` and the first manifest write — so a longer threshold applies and
    the reason code is distinct, making these easy to grep for in a log.

    Args:
        workspace: Workspace with no manifest.
        holder_job_id: Job ID currently holding the slot, or ``None``.
        now: Current time, timezone-aware.
        config: Reaper configuration.

    Returns:
        A :class:`Decision`.
    """
    if holder_job_id is not None and holder_job_id == workspace.job_id:
        return Decision(action=Action.SKIP, reason="holds-slot")
    if workspace.newest_mtime is None:
        return Decision(action=Action.SKIP, reason="orphan-no-timestamp")

    age_hours = (now - workspace.newest_mtime).total_seconds() / 3600.0
    if age_hours < config.orphan_age_hours:
        return Decision(action=Action.SKIP, reason="orphan-too-young", age_hours=age_hours)
    return Decision(
        action=Action.REMOVE_WORKSPACE,
        reason="orphan-no-manifest",
        target=workspace.path,
        bytes_estimate=workspace.total_bytes,
        age_hours=age_hours,
    )


# ---------------------------------------------------------------------------
# The agent
# ---------------------------------------------------------------------------


class CoreReaperAgent(Agent):
    """Reports reclaimable core-dump analysis workspaces without removing them.

    One tick is one sweep: read the slot holder, scan every ``job-<id>``
    directory under the root, classify it, run each candidate through the path
    guard, and log what *would* have been removed.  A second pass engages when
    usage is above the pressure threshold, relaxing the retention age — and
    only the retention age — down to the configured floor.

    The agent holds no state between ticks beyond the last report, so it is
    safe to run from the supervisor in scheduled mode alongside a live Bamboo
    MCP, and re-running it is harmless by construction: it never writes.
    """

    def __init__(self, name: str = "core-reaper", config: Optional[CoreReaperConfig] = None) -> None:
        """Initialise the reaper.

        Args:
            name: Instance name (default: ``'core-reaper'``).
            config: Reaper configuration.  Must be supplied; the parameter is
                typed ``Optional`` only to match the signature pattern used by
                the other scripts in this package.

        Raises:
            ValueError: If *config* is ``None``.
        """
        super().__init__(name=name)
        if config is None:
            raise ValueError("CoreReaperConfig must be provided")
        self.config = config
        self._last_report: Optional[SweepReport] = None

    @property
    def last_report(self) -> Optional[SweepReport]:
        """Return the report produced by the most recent sweep.

        Returns:
            The last :class:`SweepReport`, or ``None`` before the first tick.
        """
        return self._last_report

    # ------------------------------------------------------------------
    # Agent lifecycle hooks
    # ------------------------------------------------------------------

    def _start_impl(self) -> None:
        """Validate configuration and report what the sweep will cover.

        The root is *not* created if missing — this script only ever reads,
        and a missing root simply means there is nothing to sweep.
        """
        root = self.config.root
        logger.info(
            "CoreReaperAgent started: root=%s  mode=%s  min_age=%.2fh  orphan_age=%.2fh  quota=%d bytes",
            root,
            self.config.mode,
            self.config.min_age_hours,
            self.config.orphan_age_hours,
            self.config.quota_bytes,
        )
        logger.info("REPORT-ONLY build: nothing will be removed, no matter what this run finds.")
        if not root.is_dir():
            logger.warning("Analysis root %s does not exist — sweeps will report nothing.", root)
        if not self._root_allowed():
            logger.error(
                "Analysis root %s is outside the hardcoded allowlist %s — every candidate will be refused. "
                "Widening the allowlist is a code change in agent.py.",
                root,
                list(ALLOWED_PREFIXES),
            )

    def _tick_impl(self) -> None:
        """Run one sweep and record the report."""
        self._last_report = self.sweep()

    def _stop_impl(self) -> None:
        """Release resources.  There are none."""
        logger.info("CoreReaperAgent stopped")

    def _health_details(self) -> Mapping[str, Any]:
        """Return counters from the most recent sweep.

        Returns:
            Dictionary of sweep counters, or a placeholder before the first
            tick.
        """
        if self._last_report is None:
            return {"swept": False, "report_only": True}
        report = self._last_report
        return {
            "swept": True,
            "report_only": True,
            "root": report.root,
            "usage_bytes": report.usage_bytes,
            "usage_pct": round(report.usage_pct, 2),
            "reclaimable_bytes": report.reclaimable_bytes,
            "candidates": len(report.candidates),
            "refused": len(report.refused),
            "workspaces_scanned": report.workspaces_scanned,
            "errors": len(report.errors),
        }

    # ------------------------------------------------------------------
    # Sweep
    # ------------------------------------------------------------------

    def _root_allowed(self) -> bool:
        """Return whether the configured root sits under the allowlist.

        Returns:
            True when both the literal and resolved root are under an allowed
            prefix.
        """
        prefixes = _expand_prefixes(ALLOWED_PREFIXES)
        literal = os.path.normpath(os.path.abspath(str(self.config.root)))
        resolved = os.path.realpath(str(self.config.root))
        for candidate in (literal, resolved):
            if not any(candidate == prefix or _is_under(candidate, prefix) for prefix in prefixes):
                return False
        return True

    def sweep(self, now: Optional[datetime] = None) -> SweepReport:
        """Scan the root and report what could be reclaimed.

        Args:
            now: Current time, timezone-aware.  Defaults to ``datetime.now``;
                injectable for tests.

        Returns:
            The :class:`SweepReport` for this sweep.
        """
        now = now or datetime.now(timezone.utc)
        root = self.config.root
        report = SweepReport(root=str(root), started_utc=now, quota_bytes=self.config.quota_bytes)

        if not root.is_dir():
            logger.info("Nothing to sweep: %s does not exist.", root)
            return report

        holder = read_slot_holder(root)
        if holder:
            logger.info("Slot is held by job_id=%s — that workspace is off limits this sweep.", holder)

        workspaces = self._collect(root, report)
        report.workspaces_scanned = len(workspaces)
        report.usage_bytes = sum(ws.total_bytes for ws in workspaces) + sum(u["bytes"] for u in report.unmanaged)

        deferred = self._first_pass(workspaces, holder, now, report)
        self._pressure_pass(deferred, holder, now, report)
        self._log_summary(report)
        return report

    def _collect(self, root: Path, report: SweepReport) -> List[Workspace]:
        """Build the workspace list for one sweep.

        Entries that are not ``job-<digits>`` directories are recorded as
        unmanaged and never touched, which covers stray files left behind by
        manual debugging.

        Args:
            root: Analysis root.
            report: Report to record unmanaged entries and errors into.

        Returns:
            List of :class:`Workspace` objects.
        """
        workspaces: List[Workspace] = []
        try:
            entries = sorted(root.iterdir())
        except OSError as exc:
            report.errors.append(f"cannot list {root}: {exc}")
            logger.error("Cannot list %s: %s", root, exc)
            return workspaces

        for entry in entries:
            try:
                if entry.is_dir() and not entry.is_symlink() and WORKSPACE_RE.match(entry.name):
                    workspaces.append(self._build_workspace(entry))
                elif entry.name != LOCK_NAME:
                    size = directory_usage_bytes(entry) if entry.is_dir() else _safe_size(entry)
                    report.unmanaged.append({"path": str(entry), "bytes": size})
            except OSError as exc:
                report.errors.append(f"cannot inspect {entry}: {exc}")
                logger.warning("Cannot inspect %s: %s", entry, exc)
        return workspaces

    def _build_workspace(self, path: Path) -> Workspace:
        """Read one workspace directory into a :class:`Workspace`.

        Args:
            path: Workspace directory.

        Returns:
            Populated workspace.
        """
        manifest = Manifest.load(path / MANIFEST_NAME)
        job_dir = path / JOB_SUBDIR
        is_symlink = job_dir.is_symlink()
        has_job_dir = job_dir.is_dir() and not is_symlink
        if is_symlink:
            logger.warning(
                "%s is a symlink — refusing to treat it as a reclaimable job directory.", job_dir
            )
        return Workspace(
            path=path,
            job_id=path.name[len("job-"):],
            manifest=manifest,
            total_bytes=directory_usage_bytes(path),
            job_dir_bytes=directory_usage_bytes(job_dir) if has_job_dir else 0,
            newest_mtime=newest_mtime(path) if not manifest.readable else None,
            has_job_dir=has_job_dir,
            job_dir_is_symlink=is_symlink,
        )

    def _first_pass(
        self,
        workspaces: Sequence[Workspace],
        holder: Optional[str],
        now: datetime,
        report: SweepReport,
    ) -> List[Workspace]:
        """Classify every workspace at the configured retention age.

        Args:
            workspaces: Workspaces to classify.
            holder: Job ID holding the slot, or ``None``.
            now: Current time.
            report: Report to accumulate into.

        Returns:
            Workspaces that failed only the age rule, ordered oldest first, as
            input for the pressure pass.
        """
        deferred: List[Tuple[float, Workspace]] = []
        for workspace in workspaces:
            decision = classify(workspace, holder, now, self.config)
            if decision.action is Action.SKIP:
                self._record_skip(report, workspace, decision)
                if decision.young_only and (decision.age_hours or 0.0) >= self.config.min_age_floor_hours:
                    deferred.append((decision.age_hours or 0.0, workspace))
                continue
            self._record_candidate(report, workspace, decision, pressure=False)
        deferred.sort(key=lambda item: item[0], reverse=True)
        return [workspace for _, workspace in deferred]

    def _pressure_pass(
        self,
        deferred: Sequence[Workspace],
        holder: Optional[str],
        now: datetime,
        report: SweepReport,
    ) -> None:
        """Report additional candidates when usage is above the pressure mark.

        Pressure relaxes the retention age and nothing else.  Terminal state,
        slot ownership and worker liveness are re-checked through
        :func:`classify` exactly as in the first pass, and the age floor still
        applies, so an analysis that finished moments ago is never reached.

        Args:
            deferred: Age-blocked workspaces, oldest first.
            holder: Job ID holding the slot, or ``None``.
            now: Current time.
            report: Report to accumulate into.
        """
        if self.config.quota_bytes <= 0 or not deferred:
            return
        pressure_bytes = self.config.quota_bytes * self.config.pressure_pct / 100.0
        if report.usage_bytes < pressure_bytes:
            return
        target_bytes = self.config.quota_bytes * self.config.target_pct / 100.0
        logger.warning(
            "Usage %d bytes (%.1f%% of quota) is at or above the pressure mark of %.1f%% — "
            "relaxing the retention age down to the %.2fh floor.",
            report.usage_bytes,
            report.usage_pct,
            self.config.pressure_pct,
            self.config.min_age_floor_hours,
        )

        projected = report.usage_bytes - report.reclaimable_bytes
        for workspace in deferred:
            if projected <= target_bytes:
                break
            decision = classify(workspace, holder, now, self.config, min_age_hours=self.config.min_age_floor_hours)
            if decision.action is Action.SKIP:
                continue
            self._record_candidate(report, workspace, decision, pressure=True)
            projected -= decision.bytes_estimate

    def _record_skip(self, report: SweepReport, workspace: Workspace, decision: Decision) -> None:
        """Record a workspace that will be left alone.

        Args:
            report: Report to update.
            workspace: Workspace that was skipped.
            decision: Decision explaining the skip.
        """
        report.skipped[decision.reason] = report.skipped.get(decision.reason, 0) + 1
        logger.debug(
            "Keeping %s: reason=%s age=%s",
            workspace.path,
            decision.reason,
            "unknown" if decision.age_hours is None else f"{decision.age_hours:.2f}h",
        )

    def _record_candidate(self, report: SweepReport, workspace: Workspace, decision: Decision, pressure: bool) -> None:
        """Run a candidate through the path guard and log the report line.

        Args:
            report: Report to update.
            workspace: Workspace under consideration.
            decision: Decision naming the target.
            pressure: Whether this candidate came from the pressure pass.
        """
        target = decision.target
        if target is None:
            return
        kind = "job_dir" if decision.action is Action.PRUNE_JOB_DIR else "workspace"
        try:
            assert_reclaimable_path(target, self.config.root, kind)
        except UnsafePathError as exc:
            report.refused.append({"path": str(target), "job_id": workspace.job_id, "guard": str(exc)})
            logger.error("REFUSED by path guard, not reporting as reclaimable: %s", exc)
            return

        report.candidates.append(
            {
                "job_id": workspace.job_id,
                "path": str(target),
                "mode": decision.action.value,
                "state": workspace.manifest.state,
                "reason": decision.reason,
                "age_hours": None if decision.age_hours is None else round(decision.age_hours, 2),
                "bytes": decision.bytes_estimate,
                "pressure": pressure,
            }
        )
        report.reclaimable_bytes += decision.bytes_estimate
        logger.info(
            "I could have removed: %s  (job_id=%s state=%s age=%s bytes=%d mode=%s reason=%s%s)",
            target,
            workspace.job_id,
            workspace.manifest.state,
            "unknown" if decision.age_hours is None else f"{decision.age_hours:.2f}h",
            decision.bytes_estimate,
            decision.action.value,
            decision.reason,
            " pressure=yes" if pressure else "",
        )

    def _log_summary(self, report: SweepReport) -> None:
        """Emit the end-of-sweep summary.

        Args:
            report: Completed report.
        """
        logger.info(
            "Sweep complete: %d workspaces, usage=%s (%.1f%% of quota), reclaimable=%s across %d candidate(s), "
            "%d refused, %d unmanaged entries, %d error(s).  Nothing was removed.",
            report.workspaces_scanned,
            human_bytes(report.usage_bytes),
            report.usage_pct,
            human_bytes(report.reclaimable_bytes),
            len(report.candidates),
            len(report.refused),
            len(report.unmanaged),
            len(report.errors),
        )


def _safe_size(path: Path) -> int:
    """Return a file's size, or 0 when it cannot be stat'ed.

    Args:
        path: File to measure.

    Returns:
        Size in bytes.
    """
    try:
        if path.is_symlink() or not path.is_file():
            return 0
        return path.stat().st_size
    except OSError:
        return 0


def human_bytes(size: int) -> str:
    """Format a byte count for humans.

    Args:
        size: Byte count.

    Returns:
        Short string such as ``'1.0 GiB'``.
    """
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024.0 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024.0
    return f"{value:.1f} TiB"


def resolve_root(explicit: Optional[str] = None) -> Path:
    """Resolve the analysis root from an explicit value or the environment.

    Args:
        explicit: Value from ``--root``, or ``None``.

    Returns:
        Absolute path to the analysis root.
    """
    value = explicit or os.environ.get(ENV_ROOT) or DEFAULT_ROOT
    return Path(os.path.abspath(os.path.expanduser(value)))


def resolve_quota_bytes(explicit: Optional[int] = None) -> int:
    """Resolve the quota ceiling from an explicit value or the environment.

    Args:
        explicit: Value from ``--quota-bytes``, or ``None``.

    Returns:
        Quota in bytes; falls back to :data:`DEFAULT_QUOTA_BYTES` when the
        environment variable is unset or not an integer.
    """
    if explicit is not None:
        return explicit
    raw = os.environ.get(ENV_QUOTA)
    parsed = _as_int(raw)
    if raw is not None and parsed is None:
        logger.warning("%s=%r is not an integer — using the default quota.", ENV_QUOTA, raw)
    return parsed if parsed is not None else DEFAULT_QUOTA_BYTES
