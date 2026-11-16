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

"""The built wheel must carry every file the package reads at run time (S-22).

An editable install reads the source tree, so it never shows a missing data
file.  The container image installs the wheel, which before S-22 contained no
non-Python file at all: none of ``resources/config/*`` and not the dashboard's
``static/index.html``.

The wheel is built from a *copy* of the project in a temporary directory.
Building in the checkout would leave ``build/`` and ``src/*.egg-info`` behind,
and a stale ``.egg-info`` next to an editable install is known to shadow its
entry points.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

pytest.importorskip("setuptools")

REPO = Path(__file__).resolve().parents[1]
PACKAGE = "bamboo_mcp_services"

#: Files in the package directory that are not run-time data.
_NOT_DATA_SUFFIXES = (".py", ".pyc", ".pyo")


def _source_data_files() -> set[str]:
    """Return the package's non-Python files, as paths inside the wheel.

    Returns:
        Paths such as ``bamboo_mcp_services/resources/config/cric-agent.yaml``.
    """
    root = REPO / "src"
    found = set()
    for path in (root / PACKAGE).rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        if path.suffix in _NOT_DATA_SUFFIXES:
            continue
        found.add(path.relative_to(root).as_posix())
    return found


@pytest.fixture(scope="module")
def wheel_names(tmp_path_factory: pytest.TempPathFactory) -> set[str]:
    """Build a wheel from a copy of the project and return its member names.

    Args:
        tmp_path_factory: pytest's temporary-directory factory.

    Returns:
        Every member name in the built wheel.
    """
    work = tmp_path_factory.mktemp("wheel-build")
    project = work / "project"
    project.mkdir()
    for name in ("pyproject.toml", "LICENSE", "README.md"):
        shutil.copy2(REPO / name, project / name)
    shutil.copytree(
        REPO / "src", project / "src",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info"),
    )
    out = work / "dist"
    result = subprocess.run(
        [sys.executable, "-c",
         f"from setuptools import build_meta; print(build_meta.build_wheel({str(out)!r}))"],
        cwd=project, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, f"wheel build failed:\n{result.stdout}\n{result.stderr}"
    wheel = out / result.stdout.strip().splitlines()[-1]
    with zipfile.ZipFile(wheel) as zf:
        return set(zf.namelist())


def test_the_source_tree_has_data_files() -> None:
    """Guard the guard: an empty expectation would pass vacuously."""
    names = _source_data_files()
    assert f"{PACKAGE}/resources/config/supervisor-agent.yaml" in names
    assert f"{PACKAGE}/agents/dashboard_agent/static/index.html" in names


def test_wheel_contains_every_data_file(wheel_names: set[str]) -> None:
    """Every non-Python file under the package directory is in the wheel."""
    missing = sorted(_source_data_files() - wheel_names)
    assert not missing, (
        "not in the wheel — add a pattern to [tool.setuptools.package-data] "
        f"in pyproject.toml: {missing}"
    )


def test_wheel_contains_the_entry_point_modules(wheel_names: set[str]) -> None:
    """``ingestion_agent`` has no ``__init__.py``; it must still be packaged."""
    assert f"{PACKAGE}/agents/ingestion_agent/cli.py" in wheel_names
    assert f"{PACKAGE}/agents/supervisor_agent/cli.py" in wheel_names
