"""Tests for atomic file writes (S-7) and document-monitor's dot-file filter.

github-sync writes the normalised documents that document-monitor reads.  A
write in place (``Path.write_text``) truncates the file before filling it, so a
reader can see it empty or half-written.  The race test below runs the reader
in a separate process, as document-monitor is, and fails if it ever sees
anything but one complete version of the file.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from bamboo_mcp_services.agents.document_monitor_agent.agent import list_watched_files
from bamboo_mcp_services.agents.github_doc_sync_agent import github_markdown_sync
from bamboo_mcp_services.common.fs import (
    TMP_SUFFIX,
    atomic_copy,
    atomic_write_text,
    is_hidden_name,
)

SIZE = 4 * 1024 * 1024   # large enough that a non-atomic write is observable


def _leftovers(directory: Path) -> list[str]:
    """Return temporary files left in *directory*.

    Args:
        directory: Directory to inspect.

    Returns:
        Names ending in the temporary suffix.
    """
    return [p.name for p in directory.iterdir() if p.name.endswith(TMP_SUFFIX)]


# ─────────────────────────────────────────────────────────────────────────────
# atomic_write_text / atomic_copy
# ─────────────────────────────────────────────────────────────────────────────

class TestAtomicWriteText:

    def test_writes_and_replaces(self, tmp_path: Path) -> None:
        target = tmp_path / "doc.md"
        atomic_write_text(target, "first")
        atomic_write_text(target, "second ✓")
        assert target.read_text(encoding="utf-8") == "second ✓"
        assert _leftovers(tmp_path) == []

    def test_failure_leaves_the_destination_and_no_temporary_file(self, tmp_path: Path) -> None:
        target = tmp_path / "doc.md"
        target.write_text("original")
        with patch("bamboo_mcp_services.common.fs.os.fsync", side_effect=OSError("disk full")):
            with pytest.raises(OSError):
                atomic_write_text(target, "new")
        assert target.read_text() == "original"
        assert _leftovers(tmp_path) == []

    def test_keeps_the_mode_of_an_existing_file(self, tmp_path: Path) -> None:
        target = tmp_path / "doc.md"
        target.write_text("x")
        os.chmod(target, 0o640)
        atomic_write_text(target, "y")
        assert stat.S_IMODE(target.stat().st_mode) == 0o640

    def test_new_file_gets_the_mode_open_would_give(self, tmp_path: Path) -> None:
        umask = os.umask(0o022)
        try:
            atomic_write_text(tmp_path / "new.md", "x")
        finally:
            os.umask(umask)
        assert stat.S_IMODE((tmp_path / "new.md").stat().st_mode) == 0o644, "mkstemp's 0600 leaked through"

    def test_temporary_file_is_a_dot_file(self, tmp_path: Path) -> None:
        """A leftover from a killed writer must be invisible to document-monitor."""
        seen: list[str] = []
        real_replace = os.replace

        def _spy(src: str, dst: object) -> None:
            seen.append(Path(src).name)
            real_replace(src, dst)  # type: ignore[arg-type]

        with patch("bamboo_mcp_services.common.fs.os.replace", side_effect=_spy):
            atomic_write_text(tmp_path / "doc.md", "x")
        assert len(seen) == 1 and is_hidden_name(seen[0]) and seen[0].endswith(TMP_SUFFIX)


class TestAtomicCopy:

    def test_copies_content_and_mtime(self, tmp_path: Path) -> None:
        src = tmp_path / "src.md"
        src.write_text("payload")
        os.utime(src, (1_000_000_000, 1_000_000_000))
        dst = tmp_path / "dst.md"
        dst.write_text("old")
        atomic_copy(src, dst)
        assert dst.read_text() == "payload"
        assert int(dst.stat().st_mtime) == 1_000_000_000, "copy2 semantics: mtime must be preserved"
        assert _leftovers(tmp_path) == []

    def test_missing_source_leaves_the_destination(self, tmp_path: Path) -> None:
        dst = tmp_path / "dst.md"
        dst.write_text("old")
        with pytest.raises(OSError):
            atomic_copy(tmp_path / "missing.md", dst)
        assert dst.read_text() == "old"
        assert _leftovers(tmp_path) == []


# ─────────────────────────────────────────────────────────────────────────────
# The race, with a real reader process
# ─────────────────────────────────────────────────────────────────────────────

_READER = textwrap.dedent("""
    import sys, time
    path, size, seconds = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
    good = {b"a" * size, b"b" * size}
    reads = bad = 0
    end = time.monotonic() + seconds
    print("ready", flush=True)
    while time.monotonic() < end:
        with open(path, "rb") as fh:
            data = fh.read()
        reads += 1
        if data not in good:
            bad += 1
    print(reads, bad, flush=True)
""")


def _race(target: Path, write: object, seconds: float = 2.0) -> tuple[int, int]:
    """Rewrite *target* continuously while a reader process checks every read.

    Args:
        target: File to rewrite.
        write: ``write(path, text)`` callable under test.
        seconds: How long the reader runs.

    Returns:
        ``(reads, bad_reads)`` reported by the reader.
    """
    a, b = "a" * SIZE, "b" * SIZE
    atomic_write_text(target, a)
    reader = subprocess.Popen(
        [sys.executable, "-c", _READER, str(target), str(SIZE), str(seconds)],
        stdout=subprocess.PIPE, text=True,
    )
    assert reader.stdout is not None and reader.stdout.readline().strip() == "ready"
    i = 0
    while reader.poll() is None:
        write(target, a if i % 2 else b)  # type: ignore[operator]
        i += 1
    out = reader.stdout.read().split()
    assert i > 5, "writer did not get enough iterations to make the test meaningful"
    return int(out[0]), int(out[1])


class TestNoPartialReads:

    def test_reader_never_sees_a_partial_file(self, tmp_path: Path) -> None:
        reads, bad = _race(tmp_path / "doc.md", atomic_write_text)
        assert reads > 10
        assert bad == 0, f"{bad} of {reads} reads saw an empty or partial file"

    def test_the_race_is_real_without_atomic_writes(self, tmp_path: Path) -> None:
        """Control: in-place writes do produce partial reads, so the test above can fail.

        Whether the reader lands inside a write window depends on the machine
        (core count, scheduler, filesystem cache), so not observing the race is
        a skip, not a failure: it says the atomic-write test proved nothing on
        this machine, not that anything is broken.
        """
        def _in_place(path: Path, text: str) -> None:
            path.write_text(text)

        reads, bad = _race(tmp_path / "doc.md", _in_place)
        if bad == 0:
            pytest.skip(
                f"race not observed in {reads} reads on this machine; "
                "test_reader_never_sees_a_partial_file is unproven here"
            )


# ─────────────────────────────────────────────────────────────────────────────
# github-sync uses the atomic writers at every write site
# ─────────────────────────────────────────────────────────────────────────────

class TestGithubSyncWriteSites:

    def test_no_in_place_writes_remain(self) -> None:
        source = Path(github_markdown_sync.__file__).read_text()
        assert ".write_text(" not in source, "in-place write_text in github-sync"
        assert "shutil.copy" not in source, "in-place shutil copy in github-sync"
        assert source.count("atomic_write_text(") >= 4   # three normalised files + the state file
        assert source.count("atomic_copy(") >= 2         # two raw copies

    def test_state_file_is_written_atomically(self, tmp_path: Path) -> None:
        with patch.object(github_markdown_sync, "atomic_write_text", wraps=atomic_write_text) as spy:
            github_markdown_sync.save_state(tmp_path / "state" / "s.json", github_markdown_sync.SyncState())
        spy.assert_called_once()
        assert (tmp_path / "state" / "s.json").exists()


# ─────────────────────────────────────────────────────────────────────────────
# document-monitor ignores dot-files
# ─────────────────────────────────────────────────────────────────────────────

class TestDocumentMonitorSkipsDotFiles:

    def test_is_hidden_name(self) -> None:
        assert is_hidden_name(".DS_Store")
        assert is_hidden_name("/x/.doc.md.ab12.tmp")
        assert not is_hidden_name("/x/.github/template.md")
        assert not is_hidden_name("doc.md")

    def test_list_watched_files(self, tmp_path: Path) -> None:
        (tmp_path / "doc.md").write_text("x")
        (tmp_path / ".doc.md.k3j2.tmp").write_text("partial")
        (tmp_path / ".DS_Store").write_text("junk")
        (tmp_path / ".github").mkdir()
        (tmp_path / ".github" / "PULL_REQUEST_TEMPLATE.md").write_text("still indexed")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "guide.rst").write_text("x")

        names = [p.relative_to(tmp_path).as_posix() for p in list_watched_files(tmp_path)]

        assert names == [".github/PULL_REQUEST_TEMPLATE.md", "doc.md", "sub/guide.rst"]

    def test_leftover_from_a_killed_writer_is_never_listed(self, tmp_path: Path) -> None:
        with patch("bamboo_mcp_services.common.fs.os.replace", side_effect=KeyboardInterrupt):
            # KeyboardInterrupt is not an Exception: the temporary file stays, as after SIGKILL.
            with pytest.raises(KeyboardInterrupt):
                atomic_write_text(tmp_path / "doc.md", "x")
        assert _leftovers(tmp_path), "precondition: a leftover exists"
        assert list_watched_files(tmp_path) == []
