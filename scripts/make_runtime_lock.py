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

r"""Derive the runtime lock file from a ``pip freeze`` of the reference host (S-21).

The lock pins every package the installed ``bamboo-mcp-services`` needs at run
time, at exactly the version running on the reference host (aipanda033), and
nothing else.  It is computed, not hand-edited:

1. The freeze becomes a constraints file: every pinned version, minus the
   editable checkout and the CUDA packages (``nvidia-*``, ``triton``).
2. A wheel of this project is built from a temporary copy of the checkout.
3. ``pip install --dry-run --report`` resolves that wheel's full dependency
   closure for Linux x86_64, CPython 3.12, under those constraints.  The
   platform is fixed on the command line, so the result does not depend on
   the machine running this script.
4. The closure, minus this project and the CUDA packages, is written out
   sorted, one ``name==version`` per line.

Development tools in the freeze (pytest, flake8, …) drop out on their own,
because nothing in the closure needs them.  The CUDA packages are dropped
because the image installs the CPU build of the same torch version.

Usage::

    python scripts/make_runtime_lock.py --freeze services-freeze.txt \
        --source-label "aipanda033 bamboo-mcp-services env, 2026-11-13"

Step 3 needs access to PyPI.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence

REPO = Path(__file__).resolve().parents[1]

#: Default output path, relative to the repository root.
DEFAULT_OUTPUT = REPO / "requirements" / "runtime-py312.txt"

#: This project's own distribution name, normalised.
PROJECT = "bamboo-mcp-services"

#: Distribution-name prefixes of the CUDA runtime that only the CUDA torch
#: wheel pulls in.  The image installs the CPU build instead.
CUDA_PREFIXES = ("nvidia-",)

#: CUDA-only packages matched by exact (normalised) name.
CUDA_NAMES = frozenset({"triton"})

#: Target interpreter for the lock.
PYTHON_VERSION = "3.12"

#: Wheel platform tags accepted for the target.  pip's ``--platform`` does not
#: expand a manylinux tag to the older ones it implies, so every glibc level up
#: to Debian bookworm's 2.36 is listed.  duckdb 1.5.3, for one, publishes no
#: ``manylinux2014`` wheel and is unresolvable without the newer tags.
PLATFORM_TAGS = tuple(
    [f"manylinux_2_{minor}_x86_64" for minor in (36, 35, 34, 31, 28, 27, 26, 24, 17, 12, 5)]
    + ["manylinux2014_x86_64", "manylinux1_x86_64", "linux_x86_64"]
)

_FREEZE_PIN = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*==\s*([^\s;#]+)\s*$")


def normalise(name: str) -> str:
    """Return the PEP 503 normalised form of a distribution name.

    Args:
        name: A distribution name as spelled anywhere (``PyYAML``,
            ``typing_extensions``).

    Returns:
        Lower case, with runs of ``-``, ``_`` and ``.`` replaced by ``-``.
    """
    return re.sub(r"[-_.]+", "-", name).lower()


def is_cuda(name: str) -> bool:
    """Return whether *name* is a CUDA-only package.

    Args:
        name: Distribution name, in any spelling.

    Returns:
        ``True`` for ``nvidia-*`` and ``triton``.
    """
    norm = normalise(name)
    return norm in CUDA_NAMES or norm.startswith(CUDA_PREFIXES)


def constraints_from_freeze(lines: Iterable[str]) -> list[str]:
    """Turn ``pip freeze`` output into constraint lines.

    Args:
        lines: Lines of ``pip freeze`` output.

    Returns:
        ``name==version`` lines for every pinned package except CUDA ones.
        Editable installs, comments and anything not pinned with ``==`` are
        dropped.
    """
    out = []
    for line in lines:
        match = _FREEZE_PIN.match(line)
        if match is None:
            continue
        name, version = match.groups()
        if is_cuda(name):
            continue
        out.append(f"{name}=={version}")
    return out


def closure_from_report(report: Mapping) -> dict[str, str]:
    """Extract the resolved distributions from a ``pip --report`` document.

    Args:
        report: Parsed JSON of ``pip install --dry-run --report``.

    Returns:
        Normalised name to version, for every distribution pip would install
        except this project and CUDA packages.
    """
    closure = {}
    for item in report.get("install", []):
        meta = item["metadata"]
        name = normalise(meta["name"])
        if name == PROJECT or is_cuda(name):
            continue
        closure[name] = meta["version"]
    return closure


def render_lock(closure: Mapping[str, str], source_label: str) -> str:
    """Render the lock file text.

    The output depends only on its arguments, so regenerating from the same
    freeze reproduces the file byte for byte.

    Args:
        closure: Normalised name to version.
        source_label: Where the freeze came from, recorded in the header.

    Returns:
        The full file text, ending in a newline.
    """
    header = [
        "# Runtime lock for bamboo-mcp-services: Linux x86_64, CPython 3.12.",
        "# GENERATED by scripts/make_runtime_lock.py: do not edit by hand.",
        f"# Source environment: {source_label}",
        "#",
        "# Every package the installed project needs at run time, at the version",
        "# running on the reference host, and nothing else.  Install with",
        "# 'pip install --no-deps -r' and finish with 'pip check', so that a",
        "# missing entry fails the install instead of floating to a new version.",
        "#",
        "# torch is the CPU build of this version, from the PyTorch CPU index;",
        "# that is why no nvidia-* package is listed.",
        "#",
        "# duckdb and chromadb MUST equal the versions bamboo-mcp runs: both",
        "# write files that bamboo-mcp reads (S-21).",
        "",
    ]
    body = [f"{name}=={closure[name]}" for name in sorted(closure)]
    return "\n".join(header + body) + "\n"


def build_wheel(dest: Path) -> Path:
    """Build this project's wheel from a temporary copy of the checkout.

    Building in the checkout itself would leave ``build/`` and an
    ``.egg-info`` behind, which can shadow an editable install's entry points.

    Args:
        dest: Directory to write the wheel into.

    Returns:
        Path of the built wheel.

    Raises:
        RuntimeError: If the build fails.
    """
    project = dest / "project"
    project.mkdir()
    for name in ("pyproject.toml", "LICENSE", "README.md"):
        shutil.copy2(REPO / name, project / name)
    shutil.copytree(
        REPO / "src", project / "src",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info"),
    )
    out = dest / "dist"
    result = subprocess.run(
        [sys.executable, "-c",
         f"from setuptools import build_meta; print(build_meta.build_wheel({str(out)!r}))"],
        cwd=project, capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"wheel build failed:\n{result.stdout}\n{result.stderr}")
    return out / result.stdout.strip().splitlines()[-1]


def resolve(wheel: Path, constraints: Path, report: Path) -> dict:
    """Resolve *wheel*'s dependency closure for the target platform.

    Args:
        wheel: This project's wheel.
        constraints: Constraints file derived from the freeze.
        report: Where pip writes its JSON report.

    Returns:
        The parsed report.

    Raises:
        RuntimeError: If pip cannot resolve the closure under the
            constraints (for example, a version with no wheel for the target).
    """
    cmd = [
        sys.executable, "-m", "pip", "install", "--quiet", "--dry-run",
        "--ignore-installed", "--only-binary=:all:",
        "--python-version", PYTHON_VERSION, "--implementation", "cp",
        "--target", str(report.parent / "unused-target"),
        "--report", str(report), "-c", str(constraints),
    ]
    for tag in PLATFORM_TAGS:
        cmd += ["--platform", tag]
    cmd.append(str(wheel))
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"pip could not resolve the closure:\n{result.stdout}\n{result.stderr}")
    return json.loads(report.read_text(encoding="utf-8"))


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser.

    Returns:
        The configured parser.
    """
    p = argparse.ArgumentParser(
        description="Derive the runtime lock file from a pip freeze of the reference host (S-21).",
    )
    p.add_argument("--freeze", required=True, type=Path,
                   help="pip freeze output from the reference environment")
    p.add_argument("--source-label", required=True,
                   help="where the freeze came from, recorded in the file header")
    p.add_argument("--output", type=Path, default=DEFAULT_OUTPUT,
                   help="lock file to write (default: %(default)s)")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Generate the lock file.

    Args:
        argv: Command-line arguments; ``None`` means ``sys.argv[1:]``.

    Returns:
        Exit code: 0 on success, 1 on failure.
    """
    args = build_parser().parse_args(argv)
    constraints = constraints_from_freeze(args.freeze.read_text(encoding="utf-8").splitlines())
    with tempfile.TemporaryDirectory(prefix="bamboo-lock-") as tmp:
        work = Path(tmp)
        constraints_file = work / "constraints.txt"
        constraints_file.write_text("\n".join(constraints) + "\n", encoding="utf-8")
        try:
            wheel = build_wheel(work)
            report = resolve(wheel, constraints_file, work / "report.json")
        except RuntimeError as exc:
            print(exc, file=sys.stderr)
            return 1
    closure = closure_from_report(report)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render_lock(closure, args.source_label), encoding="utf-8")
    print(f"wrote {len(closure)} pins to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
