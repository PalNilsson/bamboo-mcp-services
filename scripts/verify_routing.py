#!/usr/bin/env python3
"""Verify that collection_routing.json is consistent with ChromaDB contents.

For every entry in the sidecar, the pointed-at physical collection must
exist in ChromaDB **and** contain at least one document.  An entry that
points at a missing or empty collection means RAG queries will return zero
results for that logical collection.

Usage::

    # Uses $BAMBOO_CHROMA_PATH (required):
    python scripts/verify_routing.py

    # Explicit path:
    python scripts/verify_routing.py --chroma-dir /data/.chromadb

Exit codes:
    0  All entries pass the invariant.
    1  One or more entries are broken (missing or empty collection).
    2  Usage error (missing argument or environment variable).
"""

from __future__ import annotations

import argparse
import json
import os
import sys


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Verify collection_routing.json against ChromaDB.",
    )
    p.add_argument(
        "--chroma-dir",
        default=None,
        help=(
            "ChromaDB persistence directory.  "
            "Defaults to $BAMBOO_CHROMA_PATH if set."
        ),
    )
    return p.parse_args()


def main() -> int:  # noqa: C901
    args = _parse_args()

    chroma_path = args.chroma_dir or os.environ.get("BAMBOO_CHROMA_PATH")
    if not chroma_path:
        print(
            "ERROR: --chroma-dir not given and $BAMBOO_CHROMA_PATH is not set.",
            file=sys.stderr,
        )
        return 2

    chroma_path = os.path.abspath(chroma_path)
    sidecar = os.path.join(chroma_path, "collection_routing.json")

    if not os.path.isfile(sidecar):
        print(f"ERROR: sidecar not found: {sidecar}", file=sys.stderr)
        return 2

    try:
        with open(sidecar, encoding="utf-8") as fh:
            routing: dict[str, str] = json.load(fh)
    except Exception as exc:
        print(f"ERROR: could not parse {sidecar}: {exc}", file=sys.stderr)
        return 2

    if not routing:
        print("WARNING: collection_routing.json is empty — no collections registered.")
        return 0

    try:
        import chromadb  # type: ignore
    except ImportError:
        print(
            "ERROR: chromadb is not installed.  "
            "Activate the bamboo-mcp-services conda environment first.",
            file=sys.stderr,
        )
        return 2

    try:
        client = chromadb.PersistentClient(path=chroma_path)
    except Exception as exc:
        print(f"ERROR: could not open ChromaDB at {chroma_path}: {exc}", file=sys.stderr)
        return 2

    # Snapshot all collection counts in one pass.
    try:
        counts: dict[str, int] = {col.name: col.count() for col in client.list_collections()}
    except Exception as exc:
        print(f"ERROR: could not list ChromaDB collections: {exc}", file=sys.stderr)
        return 2

    broken: list[str] = []
    for logical, physical in sorted(routing.items()):
        count = counts.get(physical, "MISSING")
        if isinstance(count, int) and count > 0:
            print(f"[OK    ] {logical:20s}  ->  {physical:30s}  ({count} docs)")
        else:
            entry = f"[BROKEN] {logical:20s}  ->  {physical:30s}  ({count} docs)"
            print(entry)
            broken.append(entry)

    print()
    if broken:
        print(f"FAIL: {len(broken)} broken entrie(s):")
        for b in broken:
            print(f"  {b}")
        print()
        print("To repair, re-run the document monitor agent with --once for the")
        print("affected collection(s), or manually update the sidecar:")
        print(f"  {sidecar}")
        return 1

    print(f"OK: all {len(routing)} routing entrie(s) are consistent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
