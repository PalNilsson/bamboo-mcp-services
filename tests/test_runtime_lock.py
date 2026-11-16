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

"""The runtime lock and its generator (S-21).

The lock is what the container image installs.  These tests check what can be
checked offline: that it pins every declared dependency exactly and inside the
range ``pyproject.toml`` allows, that no CUDA or development package slipped
in, and that the generator's filters do what the lock's header says.  That
the lock is *closed* — every dependency of every entry is itself an entry — is
checked by ``pip check`` in the image build, after ``pip install --no-deps``.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType

import pytest

tomllib = pytest.importorskip("tomllib")
_requirements = pytest.importorskip("packaging.requirements")
_utils = pytest.importorskip("packaging.utils")

REPO = Path(__file__).resolve().parents[1]
LOCK = REPO / "requirements" / "runtime-py312.txt"

_PIN = re.compile(r"^([a-z0-9]+(?:-[a-z0-9]+)*)==([0-9][0-9A-Za-z.]*)$")

#: Development tools present in the reference environment that must never be
#: shipped in the image.
_DEV_TOOLS = ("pytest", "pytest-cov", "coverage", "flake8", "pyflakes", "pycodestyle", "mccabe")


def _load_script() -> ModuleType:
    """Import ``scripts/make_runtime_lock.py``, which is not a package module.

    Returns:
        The loaded module.
    """
    path = REPO / "scripts" / "make_runtime_lock.py"
    spec = importlib.util.spec_from_file_location("make_runtime_lock", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def script() -> ModuleType:
    """The lock generator, imported from its file.

    Returns:
        The loaded module.
    """
    return _load_script()


@pytest.fixture(scope="module")
def pins() -> dict[str, str]:
    """Parse the lock file.

    Returns:
        Name to version for every pin.
    """
    found: dict[str, str] = {}
    for number, raw in enumerate(LOCK.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _PIN.match(line)
        assert match, f"{LOCK.name}:{number}: not a normalised exact pin: {raw!r}"
        name, version = match.groups()
        assert name not in found, f"{LOCK.name}:{number}: {name} pinned twice"
        found[name] = version
    return found


@pytest.fixture(scope="module")
def declared() -> list:
    """Parse ``[project].dependencies`` from ``pyproject.toml``.

    Returns:
        ``packaging.requirements.Requirement`` objects.
    """
    with open(REPO / "pyproject.toml", "rb") as fh:
        project = tomllib.load(fh)["project"]
    return [_requirements.Requirement(text) for text in project["dependencies"]]


# ── The lock file ─────────────────────────────────────────────────────────────

def test_every_declared_dependency_is_pinned_inside_its_range(pins, declared) -> None:
    """A pin outside pyproject's range would fail ``pip check`` in the image."""
    for req in declared:
        name = _utils.canonicalize_name(req.name)
        assert name in pins, f"{req.name} is declared but not in the lock"
        assert req.specifier.contains(pins[name], prereleases=True), (
            f"lock pins {name}=={pins[name]}, outside pyproject's {req.specifier}"
        )


def test_shared_format_packages_are_pinned(pins) -> None:
    """duckdb and chromadb are the cross-repository invariant with bamboo-mcp."""
    assert "duckdb" in pins
    assert "chromadb" in pins


def test_no_cuda_package(pins) -> None:
    """The image installs the CPU build of torch; CUDA libraries are dead weight."""
    cuda = sorted(name for name in pins if name.startswith("nvidia-") or name == "triton")
    assert not cuda, f"CUDA packages in the lock: {cuda}"


def test_no_development_tool(pins) -> None:
    """Nothing in the run-time closure needs a test or lint tool."""
    leaked = sorted(set(_DEV_TOOLS) & set(pins))
    assert not leaked, f"development tools in the lock: {leaked}"


def test_project_itself_is_not_pinned(pins) -> None:
    """The image installs this project from its own wheel, not from the lock."""
    assert "bamboo-mcp-services" not in pins


# ── The generator ─────────────────────────────────────────────────────────────

def test_constraints_drop_editable_and_cuda_lines(script) -> None:
    freeze = [
        "-e git+https://github.com/PalNilsson/bamboo-mcp-services.git@dc703a3#egg=bamboo_mcp_services",
        "duckdb==1.5.3",
        "nvidia-cublas-cu12==12.1.3.1",
        "triton==2.2.0",
        "# a comment",
        "torch==2.2.2",
        "",
    ]
    assert script.constraints_from_freeze(freeze) == ["duckdb==1.5.3", "torch==2.2.2"]


def test_closure_drops_the_project_and_cuda(script) -> None:
    report = {"install": [
        {"metadata": {"name": "bamboo_mcp_services", "version": "1.0.0"}},
        {"metadata": {"name": "nvidia-cudnn-cu12", "version": "8.9.2.26"}},
        {"metadata": {"name": "PyYAML", "version": "6.0.3"}},
        {"metadata": {"name": "typing_extensions", "version": "4.15.0"}},
    ]}
    assert script.closure_from_report(report) == {"pyyaml": "6.0.3", "typing-extensions": "4.15.0"}


def test_render_is_sorted_and_deterministic(script) -> None:
    closure = {"zstandard": "0.25.0", "aiohttp": "3.13.5"}
    text = script.render_lock(closure, "label")
    assert text == script.render_lock(dict(reversed(list(closure.items()))), "label")
    body = [line for line in text.splitlines() if line and not line.startswith("#")]
    assert body == ["aiohttp==3.13.5", "zstandard==0.25.0"]
    assert "# Source environment: label" in text


def test_is_cuda(script) -> None:
    assert script.is_cuda("nvidia-nccl-cu12")
    assert script.is_cuda("NVIDIA_cublas_cu12")
    assert script.is_cuda("triton")
    assert not script.is_cuda("torch")
    assert not script.is_cuda("tritonclient")
