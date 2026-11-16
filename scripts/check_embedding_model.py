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

"""Check that an embedding-model directory loads as the model the store needs (S-15).

document-monitor's ``--model-path`` must name the sentence-transformers
*snapshot directory*, the one holding ``modules.json``.  Pointed anywhere else
the model can still load, but without its ``Normalize`` module, and then gives
vectors that are wrong yet look plausible.  This script loads the directory
offline and fails unless:

* ``modules.json`` is present;
* the module chain is exactly Transformer → Pooling → Normalize;
* one sentence embeds to shape ``(1, 384)`` with unit L2 norm.

It is the image build's last step, and works the same on any host::

    python scripts/check_embedding_model.py /opt/bamboo/models/sentence-transformers/all-MiniLM-L6-v2

It does not prove the model is the one that built a given store; that is
``check_chroma_store.py``'s cosine comparison against stored vectors.

Exit status: 0 healthy, 1 a check failed, 2 the directory is unusable.
"""
from __future__ import annotations

import argparse
import math
import os
import sys
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

#: Module chain a correctly loaded all-MiniLM-L6-v2 has.
EXPECTED_MODULES = ("Transformer", "Pooling", "Normalize")

#: Embedding dimension of all-MiniLM-L6-v2.
EXPECTED_DIM = 384

#: How far the L2 norm may stray from 1.0.
NORM_TOLERANCE = 1e-3

#: Sentence embedded for the shape and norm checks.
PROBE_SENTENCE = "PanDA job failed with a stage-in timeout."


def module_names(model: Iterable[Any]) -> list[str]:
    """Return the class names of a sentence-transformers model's modules.

    Args:
        model: A ``SentenceTransformer``, which iterates over its modules.

    Returns:
        Class names in order, e.g. ``["Transformer", "Pooling", "Normalize"]``.
    """
    return [type(module).__name__ for module in model]


def check_model(model: Any, expected_dim: int = EXPECTED_DIM) -> list[str]:
    """Check a loaded model's module chain, output shape and output norm.

    Args:
        model: A loaded ``SentenceTransformer`` (or anything iterable over
            modules with an ``encode(list[str])`` method).
        expected_dim: Expected embedding dimension.

    Returns:
        One line per problem found; empty when the model is healthy.
    """
    import numpy as np

    problems = []
    names = module_names(model)
    if names != list(EXPECTED_MODULES):
        problems.append(
            f"module chain is {' -> '.join(names) or '(none)'}, expected "
            f"{' -> '.join(EXPECTED_MODULES)}; without Normalize the vectors are "
            "wrong but look plausible"
        )
    vectors = np.asarray(model.encode([PROBE_SENTENCE]))
    if vectors.shape != (1, expected_dim):
        problems.append(f"embedding shape is {vectors.shape}, expected (1, {expected_dim})")
        return problems
    norm = float(np.linalg.norm(vectors[0]))
    if not math.isfinite(norm) or abs(norm - 1.0) > NORM_TOLERANCE:
        problems.append(f"embedding L2 norm is {norm:.6f}, expected 1.0 ± {NORM_TOLERANCE}")
    return problems


def directory_problem(path: Path) -> Optional[str]:
    """Return why *path* cannot be the snapshot directory, or ``None``.

    Args:
        path: Candidate model directory.

    Returns:
        A one-line reason, or ``None`` when ``modules.json`` is present.
    """
    if not path.is_dir():
        return f"{path} is not a directory"
    if not (path / "modules.json").is_file():
        return (
            f"{path} has no modules.json — not a sentence-transformers snapshot "
            "directory (a Hugging Face cache root, with blobs/ refs/ snapshots/, is not one)"
        )
    return None


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Load the model directory offline and check it.

    Args:
        argv: Command-line arguments; ``None`` means ``sys.argv[1:]``.

    Returns:
        ``0`` if healthy, ``1`` if a check failed, ``2`` if the directory is
        not a snapshot directory.
    """
    parser = argparse.ArgumentParser(
        description="Check that an embedding-model directory loads correctly, offline.",
    )
    parser.add_argument("model_path", type=Path, help="sentence-transformers snapshot directory")
    args = parser.parse_args(argv)

    problem = directory_problem(args.model_path)
    if problem is not None:
        print(f"FAIL: {problem}", file=sys.stderr)
        return 2

    # Never reach for the Hub: a fallback download would hide a broken directory.
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(str(args.model_path), device="cpu")
    problems = check_model(model)
    print(f"model:   {args.model_path}")
    print(f"modules: {' -> '.join(module_names(model))}")
    for line in problems:
        print(f"FAIL: {line}", file=sys.stderr)
    if problems:
        return 1
    print(f"OK: shape (1, {EXPECTED_DIM}), unit norm")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
