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

"""Tests for scripts/fetch_model.py and scripts/check_embedding_model.py (S-15).

Both guard the one input that silently corrupts the vector store when wrong:
the embedding model.  Everything here runs offline — downloads are replaced by
a function that writes files, and the model by a stand-in object.
"""
from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Sequence

import pytest

np = pytest.importorskip("numpy")

REPO = Path(__file__).resolve().parents[1]
REVISION = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"


def _load(name: str) -> ModuleType:
    """Import a module from ``scripts/``.

    Args:
        name: File stem.

    Returns:
        The loaded module.
    """
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fetch = _load("fetch_model")
check = _load("check_embedding_model")

FILES = {
    "config.json": b'{"hidden_size": 384}',
    "modules.json": b"[]",
    "1_Pooling/config.json": b'{"pooling_mode_mean_tokens": true}',
}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _manifest(files=FILES, prefix: str = "./") -> str:
    """Render a manifest in ``sha256sum`` format.

    Args:
        files: Path to content.
        prefix: Path prefix as ``find .`` produces it.

    Returns:
        Manifest text.
    """
    return "".join(f"{_sha(data)}  {prefix}{name}\n" for name, data in sorted(files.items()))


class _Writer:
    """A fake downloader that records its calls and writes fixed files."""

    def __init__(self, files=FILES):
        """Remember what to write.

        Args:
            files: Path to content written on every call, whatever is requested.
        """
        self.files = files
        self.calls: list = []

    def __call__(self, repo: str, revision: str, names: Sequence[str], dest: Path) -> None:
        """Record the call and write the files under *dest*.

        Args:
            repo: Repository id.
            revision: Commit hash.
            names: Files requested.
            dest: Destination directory.
        """
        self.calls.append((repo, revision, list(names)))
        for name, data in self.files.items():
            target = dest / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)


# ── fetch_model: manifest parsing ─────────────────────────────────────────────

def test_parse_manifest_reads_sha256sum_output() -> None:
    parsed = fetch.parse_manifest(_manifest())
    assert parsed == {name: _sha(data) for name, data in FILES.items()}


def test_parse_manifest_accepts_binary_mode_and_no_prefix() -> None:
    text = f"{_sha(b'x').upper()} *config.json\n"
    assert fetch.parse_manifest(text) == {"config.json": _sha(b"x")}


@pytest.mark.parametrize("text, words", [
    ("", "no files"),
    ("# only a comment\n", "no files"),
    ("abc  config.json\n", "not '<sha256>"),
    (f"{_sha(b'x')}\n", "not '<sha256>"),
    (f"{_sha(b'x')}  /etc/passwd\n", "relative"),
    (f"{_sha(b'x')}  ../outside.json\n", "relative"),
    (f"{_sha(b'x')}  a/../../outside.json\n", "relative"),
    (f"{_sha(b'x')}  ./a.json\n{_sha(b'y')}  a.json\n", "twice"),
])
def test_parse_manifest_rejects(text: str, words: str) -> None:
    with pytest.raises(fetch.ManifestError, match=words):
        fetch.parse_manifest(text)


# ── fetch_model: verification ─────────────────────────────────────────────────

def _tree(root: Path, files=FILES) -> None:
    for name, data in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(data)


def test_verify_tree_accepts_an_exact_match(tmp_path: Path) -> None:
    _tree(tmp_path)
    assert fetch.verify_tree(tmp_path, fetch.parse_manifest(_manifest())) == []


def test_verify_tree_reports_missing_extra_and_changed(tmp_path: Path) -> None:
    files = dict(FILES)
    del files["modules.json"]
    files["config.json"] = b"tampered"
    files["extra.bin"] = b"!"
    _tree(tmp_path, files)
    problems = fetch.verify_tree(tmp_path, fetch.parse_manifest(_manifest()))
    assert "missing: modules.json" in problems
    assert "not in the manifest: extra.bin" in problems
    assert any(p.startswith("hash mismatch: config.json") for p in problems)
    assert len(problems) == 3


def _run_fetch(tmp_path: Path, downloader, revision: str = REVISION, manifest: str = _manifest()):
    path = tmp_path / "model-manifest.sha256"
    path.write_text(manifest)
    dest = tmp_path / "model"
    rc = fetch.main(
        ["--repo", "sentence-transformers/all-MiniLM-L6-v2", "--revision", revision,
         "--manifest", str(path), "--dest", str(dest)],
        download=downloader,
    )
    return rc, dest


def test_main_downloads_exactly_the_manifest_at_the_revision(tmp_path: Path) -> None:
    downloader = _Writer()
    rc, dest = _run_fetch(tmp_path, downloader)
    assert rc == 0
    assert downloader.calls == [
        ("sentence-transformers/all-MiniLM-L6-v2", REVISION, sorted(FILES)),
    ]
    assert (dest / "1_Pooling" / "config.json").read_bytes() == FILES["1_Pooling/config.json"]


def test_main_fails_when_the_download_differs(tmp_path: Path) -> None:
    rc, _ = _run_fetch(tmp_path, _Writer({**FILES, "modules.json": b"other"}))
    assert rc == 1


@pytest.mark.parametrize("revision", ["main", "1110a243", REVISION.upper(), REVISION + "0"])
def test_main_requires_a_commit_hash(tmp_path: Path, revision: str) -> None:
    """A branch name can move; only a full commit hash pins the files."""
    downloader = _Writer()
    rc, _ = _run_fetch(tmp_path, downloader, revision=revision)
    assert rc == 2
    assert downloader.calls == []


def test_main_rejects_a_bad_manifest_before_downloading(tmp_path: Path) -> None:
    downloader = _Writer()
    rc, _ = _run_fetch(tmp_path, downloader, manifest="")
    assert rc == 2
    assert downloader.calls == []


def test_main_refuses_a_non_empty_destination(tmp_path: Path) -> None:
    (tmp_path / "model").mkdir()
    (tmp_path / "model" / "leftover").write_text("x")
    downloader = _Writer()
    rc, _ = _run_fetch(tmp_path, downloader)
    assert rc == 2
    assert downloader.calls == []


# ── check_embedding_model ─────────────────────────────────────────────────────

class Transformer:
    pass


class Pooling:
    pass


class Normalize:
    pass


class _FakeModel:
    """Iterates over modules and encodes to a fixed vector, like SentenceTransformer."""

    def __init__(self, modules, vector):
        self._modules = modules
        self._vector = np.asarray(vector, dtype=float)

    def __iter__(self):
        return iter(self._modules)

    def encode(self, sentences):
        return np.stack([self._vector for _ in sentences])


def _unit(dim: int = 384):
    vec = np.arange(1, dim + 1, dtype=float)
    return vec / np.linalg.norm(vec)


def test_healthy_model_passes() -> None:
    model = _FakeModel([Transformer(), Pooling(), Normalize()], _unit())
    assert check.check_model(model) == []


def test_missing_normalize_fails_even_with_a_unit_vector() -> None:
    """The defect this guards: the chain, not just the norm, is checked."""
    model = _FakeModel([Transformer(), Pooling()], _unit())
    problems = check.check_model(model)
    assert len(problems) == 1
    assert "Normalize" in problems[0]


def test_non_unit_norm_fails() -> None:
    model = _FakeModel([Transformer(), Pooling(), Normalize()], _unit() * 1.01)
    problems = check.check_model(model)
    assert len(problems) == 1
    assert "norm" in problems[0]


def test_norm_within_tolerance_passes() -> None:
    model = _FakeModel([Transformer(), Pooling(), Normalize()], _unit() * (1 + check.NORM_TOLERANCE / 2))
    assert check.check_model(model) == []


def test_nan_vector_fails() -> None:
    model = _FakeModel([Transformer(), Pooling(), Normalize()], np.full(384, np.nan))
    assert any("norm" in p for p in check.check_model(model))


def test_wrong_dimension_fails() -> None:
    model = _FakeModel([Transformer(), Pooling(), Normalize()], _unit(8))
    problems = check.check_model(model)
    assert any("shape" in p for p in problems)


def test_directory_without_modules_json_is_rejected(tmp_path: Path) -> None:
    """A Hugging Face cache root (blobs/ refs/ snapshots/) is not a snapshot directory."""
    for name in ("blobs", "refs", "snapshots"):
        (tmp_path / name).mkdir()
    assert "modules.json" in (check.directory_problem(tmp_path) or "")
    assert check.main([str(tmp_path)]) == 2


def test_directory_with_modules_json_is_accepted(tmp_path: Path) -> None:
    (tmp_path / "modules.json").write_text("[]")
    assert check.directory_problem(tmp_path) is None


def test_missing_directory_is_rejected(tmp_path: Path) -> None:
    assert check.main([str(tmp_path / "absent")]) == 2
