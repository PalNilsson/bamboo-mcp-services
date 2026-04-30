"""GitHub documentation sync library — vendored from github-documentation-sync.

This module is a verbatim copy of ``github_markdown_sync.py`` from the
standalone ``github-documentation-sync`` project (MIT licence).  It is
vendored here so that ``bamboo-mcp-services`` has no external dependency on an
unpublished package.  The only change from the original is this module
docstring.

Upstream source: https://github.com/nilsnilsson/github-documentation-sync
"""

from __future__ import annotations

import fnmatch
import json
import logging
import logging.handlers
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
import yaml

logger = logging.getLogger(__name__)


@dataclass
class SyncState:
    """Persisted sync state for one repository.

    Attributes:
        last_commit_sha: Last synced commit SHA.
        last_sync_time: Timestamp of last sync (ISO format).
        files_downloaded: Number of files downloaded in last sync.
    """

    last_commit_sha: Optional[str] = None
    last_sync_time: Optional[str] = None
    files_downloaded: int = 0


@dataclass
class RepoConfig:
    """Configuration for a single repository.

    Attributes:
        name: Repository in 'owner/repo' format.  Used for logging, directory
            naming, and RAG metadata.  For git-clone repos this does not need
            to match a real GitHub owner/repo — any ``owner/label`` that is
            valid as a filesystem path component works.
        destination: Directory for raw downloaded files.
        normalized_destination: Directory for RAG-normalized files.
        within_hours: Skip if latest commit is older than this many hours.
        branch: Branch to sync (None = repo default).  Honoured for both the
            GitHub REST path and the git-clone path; ignored for wiki repos.
        include_patterns: Glob patterns; only matching files are synced.
        exclude_patterns: Glob patterns; matching files are excluded.
        normalize_for_rag: Prepend metadata frontmatter and convert RST->MD.
        wiki: If True, clone the GitHub wiki at
            ``https://github.com/{owner}/{parent}.wiki.git`` instead of using
            the REST API.  The ``name`` field must end with ``.wiki``.
        git: If True, clone an arbitrary git repository specified by
            ``clone_url`` instead of using the GitHub REST API.  Use this for
            non-GitHub hosts (GitLab, FramaGit, Bitbucket, etc.).
        clone_url: HTTPS clone URL used when ``git=True``.  Required when
            ``git=True``; ignored otherwise.
    """

    name: str
    destination: str
    normalized_destination: Optional[str] = None
    within_hours: Optional[int] = None
    branch: Optional[str] = None
    include_patterns: List[str] = field(default_factory=list)
    exclude_patterns: List[str] = field(default_factory=list)
    normalize_for_rag: bool = False
    wiki: bool = False
    git: bool = False
    clone_url: Optional[str] = None


def parse_repo(repo: str) -> Tuple[str, str]:
    """Parse 'owner/repo' string.

    Args:
        repo: Repository string in 'owner/repo' format.

    Returns:
        Tuple of (owner, repo_name).

    Raises:
        ValueError: If format is invalid or either segment is empty.
    """
    parts = repo.split("/", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(f"Repository must be 'owner/repo', got: {repo!r}")
    return parts[0], parts[1]


def get_latest_commit(
    owner: str, repo: str, branch: Optional[str] = None
) -> Tuple[str, datetime]:
    """Fetch latest commit SHA and datetime from GitHub API.

    Args:
        owner: Repository owner.
        repo: Repository name.
        branch: Optional branch/ref to query.

    Returns:
        Tuple of (commit_sha, commit_datetime).

    Raises:
        RuntimeError: On HTTP errors, network failures, or empty repository.
    """
    url = f"https://api.github.com/repos/{owner}/{repo}/commits"
    params: Dict[str, Any] = {"per_page": 1}
    if branch:
        params["sha"] = branch
    try:
        r = requests.get(url, params=params, timeout=30)
        r.raise_for_status()
    except requests.HTTPError as exc:
        status = exc.response.status_code
        hint = (
            f" (is the branch name '{branch}' correct?)"
            if status == 404 and branch
            else ""
        )
        raise RuntimeError(
            f"GitHub API error for {owner}/{repo}: {status} {exc.response.reason}{hint}"
        ) from exc
    except requests.RequestException as exc:
        raise RuntimeError(f"Network error fetching {owner}/{repo}: {exc}") from exc

    commits = r.json()
    if not commits:
        raise RuntimeError(f"Repository {owner}/{repo} has no commits")

    data = commits[0]
    sha = data["sha"]
    dt = datetime.fromisoformat(
        data["commit"]["committer"]["date"].replace("Z", "+00:00")
    )
    return sha, dt


def load_state(path: Path) -> SyncState:
    """Load sync state from disk.

    Args:
        path: Path to state file.

    Returns:
        SyncState instance, or empty SyncState on any read/parse failure.
    """
    if not path.exists():
        return SyncState()
    try:
        raw = json.loads(path.read_text())
        known_fields = set(SyncState.__dataclass_fields__)
        return SyncState(**{k: v for k, v in raw.items() if k in known_fields})
    except (json.JSONDecodeError, TypeError) as exc:
        logger.warning("Could not load state from %s: %s — starting fresh", path, exc)
        return SyncState()


def save_state(path: Path, state: SyncState) -> None:
    """Persist sync state to disk.

    Args:
        path: Path to state file.
        state: SyncState to persist.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state.__dict__, indent=2))


def normalize_text(
    content: str,
    *,
    source_repo: str,
    source_path: str,
    commit_sha: str,
) -> str:
    """Normalize document text for RAG, prepending metadata frontmatter.

    Args:
        content: Raw file text.
        source_repo: Repository in 'owner/repo' format.
        source_path: File path within the repository.
        commit_sha: Commit SHA the file was fetched from.

    Returns:
        Normalized text with YAML frontmatter header.
    """
    ext = Path(source_path).suffix.lower().lstrip(".")
    source_type = ext if ext in ("md", "rst") else "unknown"

    header = (
        f"---\n"
        f"source_repo: {source_repo}\n"
        f"source_path: {source_path}\n"
        f"source_type: {source_type}\n"
        f"source_commit_sha: {commit_sha}\n"
        f"---\n\n"
    )

    body = content.strip()
    if source_type == "rst":
        body = _rst_to_md(body)

    return header + body + "\n"


def _rst_to_md(text: str) -> str:
    """Basic RST → Markdown conversion for common patterns."""
    underline_chars: Dict[str, str] = {
        "=": "#", "-": "##", "~": "###", "^": "####", '"': "#####", "'": "######",
    }
    lines = text.splitlines()
    result: List[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]

        # Section headings: text followed by underline of =, -, ~, etc.
        if i + 1 < len(lines):
            nxt = lines[i + 1]
            if (
                nxt
                and line.strip()
                and len(nxt) >= len(line.strip())
                and re.match(r'^[=\-~^"\'`#*+<>]+$', nxt)
            ):
                prefix = underline_chars.get(nxt[0], "#")
                result.append(f"{prefix} {line.strip()}")
                i += 2
                continue

        # .. code-block:: lang
        code_match = re.match(r"\.\.\s+code-block::\s*(.*)", line)
        if code_match:
            lang = code_match.group(1).strip()
            result.append(f"```{lang}")
            i += 1
            if i < len(lines) and not lines[i].strip():
                i += 1
            while i < len(lines) and (
                lines[i].startswith("   ") or lines[i].startswith("\t") or not lines[i].strip()
            ):
                result.append(re.sub(r"^   |\t", "", lines[i], count=1))
                i += 1
            result.append("```")
            continue

        # .. note:: / .. warning:: / .. tip::
        admonition = re.match(r"\.\.\s+(note|warning|tip|important)::(.*)", line, re.IGNORECASE)
        if admonition:
            kind = admonition.group(1).capitalize()
            extra = admonition.group(2).strip()
            result.append(f"> **{kind}:** {extra}")
            i += 1
            continue

        # :ref:`text <target>` and `text <url>`_
        line = re.sub(r":ref:`([^`<]+)\s+<([^>]+)>`", r"[\1](\2)", line)
        line = re.sub(r"`([^`]+)\s+<([^>]+)>`_", r"[\1](\2)", line)

        result.append(line)
        i += 1

    return "\n".join(result)


def _matches_patterns(
    path: str, include: List[str], exclude: List[str]
) -> bool:
    """Return True if path matches include patterns and not exclude patterns."""
    if include and not any(fnmatch.fnmatch(path, pat) for pat in include):
        return False
    if any(fnmatch.fnmatch(path, pat) for pat in exclude):
        return False
    return True


def _get_tree(owner: str, repo: str, sha: str) -> List[Dict[str, Any]]:
    """Fetch the full recursive blob list for a commit tree.

    Args:
        owner: Repository owner.
        repo: Repository name.
        sha: Commit or tree SHA.

    Returns:
        List of blob entries (dicts with 'path', 'sha', 'type', etc.).

    Raises:
        RuntimeError: On HTTP or network errors.
    """
    url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/{sha}"
    try:
        r = requests.get(url, params={"recursive": "1"}, timeout=30)
        r.raise_for_status()
    except requests.HTTPError as exc:
        raise RuntimeError(
            f"GitHub API error fetching tree: {exc.response.status_code}"
        ) from exc
    except requests.RequestException as exc:
        raise RuntimeError(f"Network error fetching tree: {exc}") from exc

    data = r.json()
    if data.get("truncated"):
        logger.warning("Tree for %s/%s is truncated (very large repo)", owner, repo)
    return [item for item in data.get("tree", []) if item.get("type") == "blob"]


def _download_file(owner: str, repo: str, path: str, sha: str) -> bytes:
    """Download raw file content from GitHub.

    Args:
        owner: Repository owner.
        repo: Repository name.
        path: File path within the repository.
        sha: Commit SHA to fetch from.

    Returns:
        Raw file bytes.

    Raises:
        RuntimeError: On HTTP or network errors.
    """
    url = f"https://raw.githubusercontent.com/{owner}/{repo}/{sha}/{path}"
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
    except requests.HTTPError as exc:
        raise RuntimeError(
            f"Failed to download {path}: {exc.response.status_code}"
        ) from exc
    except requests.RequestException as exc:
        raise RuntimeError(f"Network error downloading {path}: {exc}") from exc
    return r.content


def _git_clone_head_sha(clone_dir: Path) -> str:
    """Return the HEAD commit SHA of a git repository on disk.

    Args:
        clone_dir: Path to the cloned repository root.

    Returns:
        Full commit SHA string.

    Raises:
        RuntimeError: If git command fails.
    """
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=clone_dir,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git rev-parse HEAD failed: {result.stderr.strip()}"
        )
    return result.stdout.strip()


def _git_clone_head_datetime(clone_dir: Path) -> datetime:
    """Return the committer datetime of HEAD in a cloned repository.

    Args:
        clone_dir: Path to the cloned repository root.

    Returns:
        UTC-aware datetime of the HEAD commit.

    Raises:
        RuntimeError: If git command fails or output cannot be parsed.
    """
    result = subprocess.run(
        ["git", "log", "-1", "--format=%cI"],
        cwd=clone_dir,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git log failed: {result.stderr.strip()}"
        )
    raw = result.stdout.strip()
    try:
        return datetime.fromisoformat(raw)
    except ValueError as exc:
        raise RuntimeError(f"Could not parse commit date {raw!r}: {exc}") from exc


def _wiki_clone_url(owner: str, parent_repo: str) -> str:
    """Build the HTTPS clone URL for a GitHub wiki.

    Args:
        owner: Repository owner.
        parent_repo: Parent repository name (without ``.wiki`` suffix).

    Returns:
        HTTPS clone URL string.
    """
    return f"https://github.com/{owner}/{parent_repo}.wiki.git"


def sync_wiki_repo(cfg: RepoConfig) -> None:
    """Sync a GitHub wiki repository by cloning it with git.

    GitHub wiki repos are not accessible via the standard REST API — they
    exist only as plain git repositories at
    ``https://github.com/{owner}/{repo}.wiki.git``.  This function clones
    (or re-clones) the wiki into a temporary directory, reads the HEAD SHA
    and commit date for change detection, then copies matching files to
    ``cfg.destination`` and optionally normalizes them into
    ``cfg.normalized_destination``, exactly like :func:`sync_repo`.

    The ``name`` field in *cfg* must be ``owner/repo.wiki``; the parent repo
    name is derived by stripping the ``.wiki`` suffix.

    Args:
        cfg: Repository configuration.  ``cfg.wiki`` must be ``True``.

    Raises:
        ValueError: If repo name is invalid or missing ``.wiki`` suffix.
        RuntimeError: On git clone failures or I/O errors.
    """
    owner, repo_name = parse_repo(cfg.name)
    if not repo_name.endswith(".wiki"):
        raise ValueError(
            f"Wiki repo name must end with '.wiki', got: {cfg.name!r}"
        )
    parent_repo = repo_name[: -len(".wiki")]
    clone_url = _wiki_clone_url(owner, parent_repo)

    dest_root = Path(cfg.destination)
    dest = dest_root / owner / repo_name
    dest.mkdir(parents=True, exist_ok=True)
    state_path = dest / ".sync_state.json"
    state = load_state(state_path)

    # Clone into a temp dir so we can inspect HEAD before committing to disk.
    with tempfile.TemporaryDirectory(prefix="bamboo_wiki_") as tmpdir:
        clone_dir = Path(tmpdir) / "clone"
        logger.debug("%s: cloning wiki from %s", cfg.name, clone_url)
        result = subprocess.run(
            ["git", "clone", "--depth", "1", clone_url, str(clone_dir)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"git clone failed for {cfg.name}: {result.stderr.strip()}"
            )

        sha = _git_clone_head_sha(clone_dir)
        commit_dt = _git_clone_head_datetime(clone_dir)

        if cfg.within_hours is not None and state.last_commit_sha is not None:
            now = datetime.now(tz=timezone.utc)
            age_hours = (now - commit_dt).total_seconds() / 3600
            if age_hours > cfg.within_hours:
                logger.info(
                    "%s: latest commit is %.1fh old (limit %dh) — skipping",
                    cfg.name, age_hours, cfg.within_hours,
                )
                return

        if sha == state.last_commit_sha:
            logger.info("%s: already up-to-date at %s", cfg.name, sha[:12])
            return

        logger.info(
            "%s: syncing %s → %s",
            cfg.name,
            (state.last_commit_sha or "none")[:12],
            sha[:12],
        )

        # Collect all blobs from the clone dir (excluding .git).
        all_files = [
            p.relative_to(clone_dir)
            for p in clone_dir.rglob("*")
            if p.is_file() and ".git" not in p.parts
        ]
        matching = [
            f for f in all_files
            if _matches_patterns(str(f), cfg.include_patterns, cfg.exclude_patterns)
        ]
        logger.info("%s: %d matching files to copy", cfg.name, len(matching))

        norm_dest = None
        if cfg.normalized_destination:
            norm_dest = Path(cfg.normalized_destination) / owner / repo_name
            norm_dest.mkdir(parents=True, exist_ok=True)

        downloaded = 0
        for rel_path in matching:
            src = clone_dir / rel_path
            out_path = dest / rel_path
            out_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, out_path)
            downloaded += 1

            if cfg.normalize_for_rag and norm_dest:
                try:
                    text = src.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    logger.warning(
                        "Skipping normalization of %s (not UTF-8)", rel_path
                    )
                    continue
                normalized = normalize_text(
                    text,
                    source_repo=cfg.name,
                    source_path=str(rel_path),
                    commit_sha=sha,
                )
                norm_path = norm_dest / rel_path
                norm_path.parent.mkdir(parents=True, exist_ok=True)
                norm_path.write_text(normalized, encoding="utf-8")

    new_state = SyncState(
        last_commit_sha=sha,
        last_sync_time=datetime.now(tz=timezone.utc).isoformat(),
        files_downloaded=downloaded,
    )
    save_state(state_path, new_state)
    logger.info("%s: done, %d files saved", cfg.name, downloaded)


def sync_git_repo(cfg: RepoConfig) -> None:
    """Sync an arbitrary git repository by cloning it with git.

    Unlike :func:`sync_wiki_repo`, which is specific to GitHub wikis, this
    function works with any publicly-accessible git repository: GitLab,
    FramaGit, Bitbucket, Gitea, or any other host.  The clone URL must be
    supplied explicitly via ``cfg.clone_url``.

    The ``branch`` field is respected: when set, the clone uses
    ``git clone -b {branch} --depth 1``; otherwise the remote default branch
    is used.

    All other behaviour — ``within_hours`` gating, SHA-unchanged skip, glob
    filtering, file copy, and RAG normalisation — is identical to
    :func:`sync_wiki_repo`.

    Args:
        cfg: Repository configuration.  ``cfg.git`` must be ``True`` and
            ``cfg.clone_url`` must be a non-empty string.

    Raises:
        ValueError: If ``clone_url`` is missing.
        RuntimeError: On git clone failures or I/O errors.
    """
    if not cfg.clone_url:
        raise ValueError(
            f"git=True requires clone_url to be set for repo {cfg.name!r}"
        )

    owner, repo_name = parse_repo(cfg.name)
    dest_root = Path(cfg.destination)
    dest = dest_root / owner / repo_name
    dest.mkdir(parents=True, exist_ok=True)
    state_path = dest / ".sync_state.json"
    state = load_state(state_path)

    clone_cmd = ["git", "clone", "--depth", "1"]
    if cfg.branch:
        clone_cmd += ["-b", cfg.branch]
    clone_cmd += [cfg.clone_url]

    with tempfile.TemporaryDirectory(prefix="bamboo_git_") as tmpdir:
        clone_dir = Path(tmpdir) / "clone"
        clone_cmd.append(str(clone_dir))
        logger.debug("%s: cloning from %s", cfg.name, cfg.clone_url)
        result = subprocess.run(clone_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(
                f"git clone failed for {cfg.name}: {result.stderr.strip()}"
            )

        sha = _git_clone_head_sha(clone_dir)
        commit_dt = _git_clone_head_datetime(clone_dir)

        if cfg.within_hours is not None and state.last_commit_sha is not None:
            now = datetime.now(tz=timezone.utc)
            age_hours = (now - commit_dt).total_seconds() / 3600
            if age_hours > cfg.within_hours:
                logger.info(
                    "%s: latest commit is %.1fh old (limit %dh) — skipping",
                    cfg.name, age_hours, cfg.within_hours,
                )
                return

        if sha == state.last_commit_sha:
            logger.info("%s: already up-to-date at %s", cfg.name, sha[:12])
            return

        logger.info(
            "%s: syncing %s -> %s",
            cfg.name,
            (state.last_commit_sha or "none")[:12],
            sha[:12],
        )

        all_files = [
            p.relative_to(clone_dir)
            for p in clone_dir.rglob("*")
            if p.is_file() and ".git" not in p.parts
        ]
        matching = [
            f for f in all_files
            if _matches_patterns(str(f), cfg.include_patterns, cfg.exclude_patterns)
        ]
        logger.info("%s: %d matching files to copy", cfg.name, len(matching))

        norm_dest = None
        if cfg.normalized_destination:
            norm_dest = Path(cfg.normalized_destination) / owner / repo_name
            norm_dest.mkdir(parents=True, exist_ok=True)

        downloaded = 0
        for rel_path in matching:
            src = clone_dir / rel_path
            out_path = dest / rel_path
            out_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, out_path)
            downloaded += 1

            if cfg.normalize_for_rag and norm_dest:
                try:
                    text_content = src.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    logger.warning(
                        "Skipping normalization of %s (not UTF-8)", rel_path
                    )
                    continue
                normalized = normalize_text(
                    text_content,
                    source_repo=cfg.name,
                    source_path=str(rel_path),
                    commit_sha=sha,
                )
                norm_path = norm_dest / rel_path
                norm_path.parent.mkdir(parents=True, exist_ok=True)
                norm_path.write_text(normalized, encoding="utf-8")

    new_state = SyncState(
        last_commit_sha=sha,
        last_sync_time=datetime.now(tz=timezone.utc).isoformat(),
        files_downloaded=downloaded,
    )
    save_state(state_path, new_state)
    logger.info("%s: done, %d files saved", cfg.name, downloaded)


def sync_repo(cfg: RepoConfig) -> None:
    """Run a full sync cycle for one repository.

    Dispatches to :func:`sync_wiki_repo` when ``cfg.wiki`` is ``True``,
    to :func:`sync_git_repo` when ``cfg.git`` is ``True``, or otherwise
    uses the standard GitHub REST API path.

    Args:
        cfg: Repository configuration.

    Raises:
        ValueError: If repo name is invalid or required fields are missing.
        RuntimeError: On GitHub API, git, or network failures.
    """
    if cfg.wiki:
        sync_wiki_repo(cfg)
        return

    if cfg.git:
        sync_git_repo(cfg)
        return

    owner, repo_name = parse_repo(cfg.name)
    dest_root = Path(cfg.destination)

    # Each repo gets its own subdirectory under dest_root so that multiple
    # repos can share a common destination without name collisions.  The state
    # file lives inside that subdirectory so it is removed along with the files
    # when the directory is deleted, and each repo's state is fully independent.
    dest = dest_root / owner / repo_name
    dest.mkdir(parents=True, exist_ok=True)
    state_path = dest / ".sync_state.json"
    state = load_state(state_path)

    sha, commit_dt = get_latest_commit(owner, repo_name, cfg.branch)

    if cfg.within_hours is not None and state.last_commit_sha is not None:
        # Only apply the recency gate when we have already synced this repo at
        # least once.  On a first run (no state file) we always download,
        # regardless of how old the latest commit is.
        now = datetime.now(tz=timezone.utc)
        age_hours = (now - commit_dt).total_seconds() / 3600
        if age_hours > cfg.within_hours:
            logger.info(
                "%s: latest commit is %.1fh old (limit %dh) — skipping",
                cfg.name, age_hours, cfg.within_hours,
            )
            return

    if sha == state.last_commit_sha:
        logger.info("%s: already up-to-date at %s", cfg.name, sha[:12])
        return

    logger.info(
        "%s: syncing %s → %s",
        cfg.name,
        (state.last_commit_sha or "none")[:12],
        sha[:12],
    )

    blobs = _get_tree(owner, repo_name, sha)
    matching = [
        b for b in blobs
        if _matches_patterns(b["path"], cfg.include_patterns, cfg.exclude_patterns)
    ]
    logger.info("%s: %d matching files to download", cfg.name, len(matching))

    # Files are written into owner/repo_name subdirectories so that output
    # from multiple repositories can share a common destination root without
    # name collisions, and so that the origin of each file is unambiguous.
    dest = dest_root / owner / repo_name
    dest.mkdir(parents=True, exist_ok=True)

    norm_dest = None
    if cfg.normalized_destination:
        norm_dest = Path(cfg.normalized_destination) / owner / repo_name
        norm_dest.mkdir(parents=True, exist_ok=True)

    downloaded = 0
    for blob in matching:
        file_path = blob["path"]
        try:
            content_bytes = _download_file(owner, repo_name, file_path, sha)
        except RuntimeError as exc:
            logger.warning("Skipping %s: %s", file_path, exc)
            continue

        out_path = dest / file_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(content_bytes)
        downloaded += 1

        if cfg.normalize_for_rag and norm_dest:
            try:
                text = content_bytes.decode("utf-8")
            except UnicodeDecodeError:
                logger.warning("Skipping normalization of %s (not UTF-8)", file_path)
                continue
            normalized = normalize_text(
                text,
                source_repo=cfg.name,
                source_path=file_path,
                commit_sha=sha,
            )
            norm_path = norm_dest / file_path
            norm_path.parent.mkdir(parents=True, exist_ok=True)
            norm_path.write_text(normalized, encoding="utf-8")

    new_state = SyncState(
        last_commit_sha=sha,
        last_sync_time=datetime.now(tz=timezone.utc).isoformat(),
        files_downloaded=downloaded,
    )
    save_state(state_path, new_state)
    logger.info("%s: done, %d files saved", cfg.name, downloaded)


def load_config(path: Path) -> Tuple[List[RepoConfig], Dict[str, Any]]:
    """Load YAML configuration file.

    Args:
        path: Path to YAML config file.

    Returns:
        Tuple of (list of RepoConfig, logging config dict).

    Raises:
        FileNotFoundError: If config file does not exist.
        KeyError: If a required field is missing from a repo entry.
    """
    raw = yaml.safe_load(path.read_text())
    logging_cfg: Dict[str, Any] = raw.get("logging", {})
    repos = [
        RepoConfig(
            name=entry["name"],
            destination=entry["destination"],
            normalized_destination=entry.get("normalized_destination"),
            within_hours=entry.get("within_hours"),
            branch=entry.get("branch"),
            include_patterns=entry.get("include_patterns", []),
            exclude_patterns=entry.get("exclude_patterns", []),
            normalize_for_rag=entry.get("normalize_for_rag", False),
            wiki=entry.get("wiki", False),
            git=entry.get("git", False),
            clone_url=entry.get("clone_url"),
        )
        for entry in raw.get("repos", [])
    ]
    return repos, logging_cfg
