# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
#
# Authors
# - Paul Nilsson, paul.nilsson@cern.ch, 2026

"""Atomic file writes.

A file written in place — ``Path.write_text`` or ``shutil.copy2`` onto an
existing path — is truncated first and filled afterwards, so a process reading
it at the wrong moment sees an empty or partial file.  document-monitor reads
the files github-sync writes, hashes them, and embeds whatever it read; a
partial read put partial content into ChromaDB until the next run.

The functions here write to a temporary file in the destination's own
directory (same filesystem, so ``os.replace`` is an atomic rename), flush and
``fsync`` it, then replace the destination.  A reader sees the old file or the
new one, never a mixture; a reader that already has the old file open keeps
reading the old one.

Temporary files are named ``.<name>.<random>.tmp``.  The leading dot keeps them
out of document-monitor, which ignores dot-files, should one be left behind by
a process killed mid-write.  Any other failure removes the temporary file.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Union

PathLike = Union[str, "os.PathLike[str]"]

#: Suffix of every temporary file created here.
TMP_SUFFIX = ".tmp"


def _temp_in_dir(dest: Path) -> tuple[int, str]:
    """Create a temporary file next to *dest*.

    Args:
        dest: Final destination path; its parent directory must exist.

    Returns:
        ``(fd, path)`` of the new, empty temporary file.
    """
    return tempfile.mkstemp(prefix=f".{dest.name}.", suffix=TMP_SUFFIX, dir=dest.parent)


def _replace_or_discard(tmp_path: str, dest: Path, ok: bool) -> None:
    """Move *tmp_path* over *dest* if *ok*, otherwise delete it.

    Args:
        tmp_path: Fully written and fsynced temporary file.
        dest: Final destination.
        ok: Whether the temporary file was written successfully.
    """
    if ok:
        os.replace(tmp_path, dest)
        return
    try:
        os.unlink(tmp_path)
    except FileNotFoundError:
        pass


def atomic_write_text(path: PathLike, text: str, encoding: str = "utf-8") -> None:
    """Write *text* to *path* so that readers never see a partial file.

    The parent directory must already exist.  The result keeps an existing
    file's permissions, or for a new file gets the permissions a plain
    ``open(path, "w")`` would give (see :func:`_match_mode`).

    Args:
        path: Destination file.
        text: Content to write.
        encoding: Text encoding.

    Raises:
        OSError: If the temporary file cannot be created, written or moved;
            the destination is left untouched.
    """
    dest = Path(path)
    fd, tmp_path = _temp_in_dir(dest)
    ok = False
    try:
        with os.fdopen(fd, "w", encoding=encoding) as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        _match_mode(tmp_path, dest)
        ok = True
    finally:
        _replace_or_discard(tmp_path, dest, ok)


def atomic_copy(src: PathLike, dst: PathLike) -> None:
    """Copy *src* to *dst* (content and metadata, like ``shutil.copy2``) atomically.

    The parent directory of *dst* must already exist.

    Args:
        src: Source file.
        dst: Destination file.

    Raises:
        OSError: If the copy fails; the destination is left untouched.
    """
    dest = Path(dst)
    fd, tmp_path = _temp_in_dir(dest)
    ok = False
    try:
        with os.fdopen(fd, "wb") as out, open(src, "rb") as inp:
            shutil.copyfileobj(inp, out)
            out.flush()
            os.fsync(out.fileno())
        shutil.copystat(src, tmp_path)
        ok = True
    finally:
        _replace_or_discard(tmp_path, dest, ok)


def _match_mode(tmp_path: str, dest: Path) -> None:
    """Give the temporary file the mode a plain ``open(dest, "w")`` would have.

    ``mkstemp`` creates files ``0600``.  Keep an existing destination's mode;
    for a new file use ``0666`` minus the process umask, as ``open`` would.

    Args:
        tmp_path: Temporary file to adjust.
        dest: Destination it will replace.
    """
    try:
        mode = dest.stat().st_mode & 0o7777
    except FileNotFoundError:
        umask = os.umask(0)
        os.umask(umask)
        mode = 0o666 & ~umask
    os.chmod(tmp_path, mode)


def is_hidden_name(path: PathLike) -> bool:
    """Return whether the file's own name starts with a dot.

    Only the final component is checked, so content under a dot-directory such
    as ``.github/`` is not affected.

    Args:
        path: Any path.

    Returns:
        ``True`` for ``.DS_Store``, ``.foo.md.x1y2.tmp`` and the like.
    """
    return Path(path).name.startswith(".")
