#!/usr/bin/env python3
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

"""Fetch the embedding model for the image, bit-identical to the reference host (S-15).

The ChromaDB store was built with a particular model snapshot, and a
different one gives different vectors.  The image therefore carries exactly
the files of the snapshot directory on aipanda033, and proves it:

1. ``docker/model-manifest.sha256`` lists every file of that directory with
   its SHA-256, in ``sha256sum`` format.  It is generated on aipanda033 (see
   ``README-container.md``) and committed.
2. This script downloads exactly those files from the Hugging Face Hub at a
   pinned **commit hash** — never a branch, which can move.
3. It then checks the destination holds those files and nothing else, each
   with its recorded hash.  Any difference fails the build.

Exit status: 0 verified, 1 verification failed, 2 bad manifest or arguments.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import sys
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping, Optional, Sequence

_HASH = re.compile(r"^[0-9a-f]{64}$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")

#: Metadata directory ``hf_hub_download(local_dir=...)`` creates; not a model file.
_HUB_METADATA_DIR = ".cache"

Downloader = Callable[[str, str, Sequence[str], Path], None]


class ManifestError(ValueError):
    """The manifest is malformed or unsafe."""


def parse_manifest(text: str) -> dict[str, str]:
    """Parse ``sha256sum`` output into relative path → hash.

    Accepts both text-mode (``hash  path``) and binary-mode (``hash *path``)
    lines, and a leading ``./`` on paths.

    Args:
        text: Manifest contents.

    Returns:
        POSIX relative path to lower-case hex SHA-256.

    Raises:
        ManifestError: On a malformed line, an absolute or escaping path, a
            duplicate path, or an empty manifest.
    """
    entries: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(maxsplit=1)
        if len(parts) != 2 or not _HASH.match(parts[0].lower()):
            raise ManifestError(f"line {number}: not '<sha256>  <path>': {raw!r}")
        digest, name = parts[0].lower(), parts[1].lstrip("*")
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts:
            raise ManifestError(f"line {number}: path must be relative and inside the model: {name!r}")
        rel = path.as_posix().removeprefix("./")
        if not rel or rel == ".":
            raise ManifestError(f"line {number}: empty path")
        if rel in entries:
            raise ManifestError(f"line {number}: {rel} listed twice")
        entries[rel] = digest
    if not entries:
        raise ManifestError("manifest lists no files")
    return entries


def sha256_of(path: Path) -> str:
    """Return the hex SHA-256 of a file.

    Args:
        path: File to hash.

    Returns:
        Lower-case hex digest.
    """
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_tree(root: Path, expected: Mapping[str, str]) -> list[str]:
    """Compare a directory with the manifest.

    Args:
        root: Directory to check.
        expected: Relative path to SHA-256, from :func:`parse_manifest`.

    Returns:
        One line per problem: missing files, extra files, and hash
        mismatches.  Empty when the directory matches exactly.
    """
    actual = {
        p.relative_to(root).as_posix(): p
        for p in root.rglob("*")
        if p.is_file() or p.is_symlink()
    }
    problems = [f"missing: {rel}" for rel in sorted(set(expected) - set(actual))]
    problems += [f"not in the manifest: {rel}" for rel in sorted(set(actual) - set(expected))]
    for rel in sorted(set(expected) & set(actual)):
        got = sha256_of(actual[rel])
        if got != expected[rel]:
            problems.append(f"hash mismatch: {rel} is {got}, manifest says {expected[rel]}")
    return problems


def hub_download(repo: str, revision: str, files: Sequence[str], dest: Path) -> None:
    """Download *files* of *repo* at *revision* into *dest* from the Hugging Face Hub.

    Args:
        repo: Repository id, e.g. ``sentence-transformers/all-MiniLM-L6-v2``.
        revision: Commit hash.
        files: Paths inside the repository.
        dest: Local directory; created if missing.
    """
    from huggingface_hub import hf_hub_download

    dest.mkdir(parents=True, exist_ok=True)
    for name in files:
        hf_hub_download(repo_id=repo, filename=name, revision=revision, local_dir=str(dest))
    shutil.rmtree(dest / _HUB_METADATA_DIR, ignore_errors=True)


def main(argv: Optional[Sequence[str]] = None, download: Downloader = hub_download) -> int:
    """Download the manifest's files and verify them.

    Args:
        argv: Command-line arguments; ``None`` means ``sys.argv[1:]``.
        download: Download function; replaced in tests.

    Returns:
        ``0`` verified, ``1`` verification failed, ``2`` bad manifest or
        revision.
    """
    parser = argparse.ArgumentParser(
        description="Download an embedding model at a pinned revision and verify it against a manifest.",
    )
    parser.add_argument("--repo", required=True, help="Hugging Face repository id")
    parser.add_argument("--revision", required=True, help="40-character commit hash")
    parser.add_argument("--manifest", required=True, type=Path, help="sha256sum-format manifest")
    parser.add_argument("--dest", required=True, type=Path, help="directory to create")
    args = parser.parse_args(argv)

    if not _REVISION.match(args.revision):
        print(f"FAIL: --revision must be a 40-character commit hash, not {args.revision!r}", file=sys.stderr)
        return 2
    try:
        expected = parse_manifest(args.manifest.read_text(encoding="utf-8"))
    except (OSError, ManifestError) as exc:
        print(f"FAIL: manifest {args.manifest}: {exc}", file=sys.stderr)
        return 2
    if args.dest.exists() and any(args.dest.iterdir()):
        print(f"FAIL: {args.dest} is not empty", file=sys.stderr)
        return 2

    download(args.repo, args.revision, sorted(expected), args.dest)
    problems = verify_tree(args.dest, expected)
    for line in problems:
        print(f"FAIL: {line}", file=sys.stderr)
    if problems:
        return 1
    print(f"OK: {len(expected)} files of {args.repo}@{args.revision[:12]} match {args.manifest.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
