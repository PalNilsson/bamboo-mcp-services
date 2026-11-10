"""Incremental cycles keep the whole corpus live (S-11).

Before S-11 every cycle rebuilt the idle slot from the changed files only and
promoted it, so after one file changed the live collection held that file and
nothing else, while the checkpoint still listed the rest as ingested.

These tests use real ChromaDB in ``tmp_path`` and a deterministic fake
embedder, and construct a new agent per cycle — what ``--once`` under the
supervisor does — so the checkpoint is re-read from disk every time.  The
store is inspected by :func:`_live`, a reader written independently of the
agent's own ``file_versions``, the way Bamboo MCP reads it: sidecar first,
then the live collection.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List

import chromadb
import numpy as np
import pytest

from bamboo_mcp_services.agents.document_monitor_agent import agent as agent_module
from bamboo_mcp_services.agents.document_monitor_agent.agent import DocumentMonitorAgent
from bamboo_mcp_services.agents.document_monitor_agent.storage import ChromaWrapper, CollectionRouter
from bamboo_mcp_services.agents.document_monitor_agent.utils import content_hash

LOGICAL = "docs"
DIM = 16


class FakeEmbedder:
    """Deterministic embedder that records what it embeds and can be made to fail.

    Attributes:
        encoded: Every chunk passed to :meth:`encode`, in order.
        fail_on: Substring that makes :meth:`encode` raise when present in a chunk.
    """

    def __init__(self, fail_on: str | None = None) -> None:
        """Create the embedder.

        Args:
            fail_on: Substring that makes :meth:`encode` raise, or None.
        """
        self.encoded: List[str] = []
        self.fail_on = fail_on

    def encode(self, texts: List[str], show_progress_bar: bool = False) -> np.ndarray:
        """Embed *texts* deterministically.

        Args:
            texts: Chunks to embed.
            show_progress_bar: Ignored.

        Returns:
            One unit vector per chunk.

        Raises:
            RuntimeError: If any chunk contains ``fail_on``.
        """
        if self.fail_on and any(self.fail_on in t for t in texts):
            raise RuntimeError("embedding failed")
        self.encoded.extend(texts)
        rows = []
        for t in texts:
            v = np.frombuffer(hashlib.sha256(t.encode()).digest()[:DIM], dtype=np.uint8).astype(float) + 1.0
            rows.append(v / np.linalg.norm(v))
        return np.array(rows)


def _doc(n: int, tag: str = "") -> str:
    """Return a document body long enough to produce several chunks.

    Args:
        n: Document number.
        tag: Extra text, used to change a document's content.

    Returns:
        Document text.
    """
    return f"Document {n} {tag}. " + " ".join(f"word{n}_{i}" for i in range(60))


class Env:
    """A watched directory, ChromaDB store and checkpoint in ``tmp_path``."""

    def __init__(self, tmp_path: Path) -> None:
        """Create the directories.

        Args:
            tmp_path: pytest temporary directory.
        """
        self.docs = tmp_path / "docs"
        self.chroma = tmp_path / "chroma"
        self.ckpt = tmp_path / "ckpt" / "checkpoints.json"
        self.docs.mkdir()

    def write(self, name: str, text: str) -> str:
        """Write a document and return its resolved path.

        Args:
            name: File name.
            text: Content.

        Returns:
            Absolute path string, as the agent records it.
        """
        p = self.docs / name
        p.write_text(text, encoding="utf-8")
        return str(p.resolve())

    def cycle(self, embedder: FakeEmbedder | None = None) -> DocumentMonitorAgent:
        """Run one cycle with a fresh agent, as ``--once`` does.

        Args:
            embedder: Embedder to use; a new FakeEmbedder when None.

        Returns:
            The agent, for inspecting its embedder and last error.
        """
        agent = DocumentMonitorAgent(
            name=LOGICAL,
            directory=str(self.docs),
            poll_interval_sec=0,
            chunk_size=80,
            chunk_overlap=10,
            checkpoint_file=str(self.ckpt),
            chroma_dir=str(self.chroma),
            embedder=embedder or FakeEmbedder(),
        )
        agent._tick_impl()
        return agent

    def checkpoint(self) -> Dict[str, Dict[str, Any]]:
        """Read the checkpoint file from disk.

        Returns:
            The ``processed`` mapping.
        """
        return json.loads(self.ckpt.read_text(encoding="utf-8"))["processed"]

    def sidecar(self) -> Dict[str, str]:
        """Read the routing sidecar.

        Returns:
            Logical name -> live physical slot.
        """
        return json.loads((self.chroma / "collection_routing.json").read_text(encoding="utf-8"))


def _live(env: Env) -> Dict[str, Dict[str, int]]:
    """Read the live slot as a reader would: ``{source_file: {content_hash: chunks}}``.

    Args:
        env: Test environment.

    Returns:
        File versions and chunk counts held by the live slot.
    """
    client = chromadb.PersistentClient(path=str(env.chroma))
    col = client.get_collection(env.sidecar()[LOGICAL])
    got = col.get(include=["metadatas"])
    out: Dict[str, Dict[str, int]] = {}
    for meta in got["metadatas"] or []:
        per = out.setdefault(str(meta["source_file"]), {})
        h = str(meta["content_hash"])
        per[h] = per.get(h, 0) + 1
    return out


@pytest.fixture
def env(tmp_path: Path) -> Env:
    """Three documents, ingested by one full cycle.

    Args:
        tmp_path: pytest temporary directory.

    Returns:
        The environment after the first cycle.
    """
    e = Env(tmp_path)
    for n in (1, 2, 3):
        e.write(f"f{n}.md", _doc(n))
    e.cycle()
    return e


def _paths(env: Env) -> List[str]:
    """Return the resolved paths of f1.md, f2.md, f3.md.

    Args:
        env: Test environment.

    Returns:
        Paths in order.
    """
    return [str((env.docs / f"f{n}.md").resolve()) for n in (1, 2, 3)]


def test_first_cycle_ingests_everything(env: Env) -> None:
    """Baseline: a full cycle leaves all files live and checkpointed."""
    assert set(_live(env)) == set(_paths(env))
    assert set(env.checkpoint()) == set(_paths(env))


def test_change_keeps_unchanged_files_live(env: Env) -> None:
    """The S-11 defect: after one file changes, the others must still be live."""
    f1, f2, f3 = _paths(env)
    before = _live(env)
    new_text = _doc(2, "edited")
    env.write("f2.md", new_text)

    embedder = FakeEmbedder()
    env.cycle(embedder)

    live = _live(env)
    assert set(live) == {f1, f2, f3}
    assert live[f1] == before[f1] and live[f3] == before[f3]
    assert set(live[f2]) == {content_hash(new_text)}
    assert env.checkpoint()[f2]["content_hash"] == content_hash(new_text)
    # Unchanged files are carried, not re-embedded.
    assert embedder.encoded and all("Document 2" in t or "word2_" in t for t in embedder.encoded)


def test_carried_records_are_identical(env: Env) -> None:
    """Carried records keep their ids, text and vectors exactly."""
    client = chromadb.PersistentClient(path=str(env.chroma))
    f1 = _paths(env)[0]

    def snapshot() -> Dict[str, Any]:
        col = client.get_collection(env.sidecar()[LOGICAL])
        got = col.get(where={"source_file": f1}, include=["embeddings", "documents", "metadatas"])
        embeddings = [] if got["embeddings"] is None else list(got["embeddings"])
        rows = zip(got["ids"], got["documents"] or [], got["metadatas"] or [], embeddings)
        return {i: (d, m, list(np.asarray(e))) for i, d, m, e in rows}

    before = snapshot()
    slot_before = env.sidecar()[LOGICAL]
    env.write("f2.md", _doc(2, "edited"))
    env.cycle()
    assert env.sidecar()[LOGICAL] != slot_before, "the cycle should have swapped slots"
    assert snapshot() == before


def test_new_file_is_added_to_full_corpus(env: Env) -> None:
    """A new file joins the existing corpus rather than replacing it."""
    f4 = env.write("f4.md", _doc(4))
    env.cycle()
    assert set(_live(env)) == set(_paths(env)) | {f4}


def test_deleted_file_is_dropped_and_forgotten(env: Env) -> None:
    """S-11.2: a deletion alone triggers a cycle; the file leaves the slot and the checkpoint."""
    f1, f2, f3 = _paths(env)
    (env.docs / "f3.md").unlink()
    env.cycle()
    assert set(_live(env)) == {f1, f2}
    assert f3 not in env.checkpoint()


def test_emptied_file_is_dropped(env: Env) -> None:
    """A file that no longer yields text is treated as removed."""
    f1, f2, f3 = _paths(env)
    env.write("f3.md", "")
    env.cycle()
    assert set(_live(env)) == {f1, f2}
    assert f3 not in env.checkpoint()


def test_no_change_does_not_rebuild(env: Env) -> None:
    """No new, changed or removed file: nothing is embedded and no swap happens."""
    slot = env.sidecar()[LOGICAL]
    embedder = FakeEmbedder()
    env.cycle(embedder)
    assert embedder.encoded == []
    assert env.sidecar()[LOGICAL] == slot


def test_failed_change_keeps_previous_version(env: Env) -> None:
    """A changed file whose embedding fails keeps its old version, live and in the checkpoint."""
    f1, f2, f3 = _paths(env)
    old = _live(env)[f2]
    old_hash = env.checkpoint()[f2]["content_hash"]
    env.write("f2.md", _doc(2, "BOOM"))
    env.write("f4.md", _doc(4))  # one success, so the cycle still swaps
    env.cycle(FakeEmbedder(fail_on="BOOM"))

    live = _live(env)
    assert live[f2] == old
    assert env.checkpoint()[f2]["content_hash"] == old_hash
    assert {f1, f3} <= set(live)

    # Next cycle retries the change and succeeds.
    env.cycle()
    assert set(_live(env)[f2]) == {content_hash(_doc(2, "BOOM"))}


def test_failed_new_file_is_absent_then_retried(env: Env) -> None:
    """A new file whose embedding fails is not live and not checkpointed, so it is retried."""
    f4 = env.write("f4.md", _doc(4, "BOOM"))
    env.write("f2.md", _doc(2, "edited"))
    env.cycle(FakeEmbedder(fail_on="BOOM"))
    assert f4 not in _live(env)
    assert f4 not in env.checkpoint()

    env.cycle()
    assert f4 in _live(env)


def test_unreadable_file_is_carried_not_dropped(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """A transient read error must not drop a document from the store."""
    f1, f2, f3 = _paths(env)
    old = _live(env)[f1]
    real = agent_module.extract_text_from_file

    def flaky(path: str) -> str:
        if path == f1:
            raise OSError("transient")
        return real(path)

    monkeypatch.setattr(agent_module, "extract_text_from_file", flaky)
    env.write("f2.md", _doc(2, "edited"))
    env.cycle()
    assert _live(env)[f1] == old
    assert f1 in env.checkpoint()


def test_gate_refuses_an_incomplete_slot(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """S-11.3: if the carry-over loses records, the slot is not promoted and nothing is recorded."""
    f1, f2, f3 = _paths(env)
    before, slot = _live(env), env.sidecar()[LOGICAL]
    old_hash = env.checkpoint()[f2]["content_hash"]
    monkeypatch.setattr(ChromaWrapper, "copy_files", lambda self, source, target, keep: {})
    env.write("f2.md", _doc(2, "edited"))

    agent = env.cycle()

    assert env.sidecar()[LOGICAL] == slot
    assert _live(env) == before
    assert env.checkpoint()[f2]["content_hash"] == old_hash
    assert agent._last_error and "not promoted" in agent._last_error


def test_checkpoint_not_ahead_when_swap_fails(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """S-11.5: a cycle that dies before the swap leaves the checkpoint matching the live slot."""
    f2 = _paths(env)[1]
    old_hash = env.checkpoint()[f2]["content_hash"]

    def boom(self: CollectionRouter, logical: str, chroma: ChromaWrapper) -> None:
        raise RuntimeError("killed before swap")

    with monkeypatch.context() as m:
        m.setattr(CollectionRouter, "commit_swap", boom)
        env.write("f2.md", _doc(2, "edited"))
        env.cycle()
    assert env.checkpoint()[f2]["content_hash"] == old_hash

    env.cycle()  # the change is still pending, so it is picked up now
    assert set(_live(env)[f2]) == {content_hash(_doc(2, "edited"))}


def test_missing_checkpointed_file_is_repaired(env: Env) -> None:
    """S-11.6: a file the checkpoint lists but the live slot lacks is re-embedded."""
    f1, f2, f3 = _paths(env)
    client = chromadb.PersistentClient(path=str(env.chroma))
    client.get_collection(env.sidecar()[LOGICAL]).delete(where={"source_file": f1})
    assert f1 not in _live(env)

    env.write("f2.md", _doc(2, "edited"))  # any change triggers a cycle
    env.cycle()
    assert set(_live(env)) == {f1, f2, f3}


def test_removing_every_file_keeps_live_slot(env: Env) -> None:
    """A cycle that would leave the collection empty does not swap."""
    before, slot = _live(env), env.sidecar()[LOGICAL]
    for n in (1, 2, 3):
        (env.docs / f"f{n}.md").unlink()
    env.cycle()
    assert env.sidecar()[LOGICAL] == slot
    assert _live(env) == before


def test_all_embeds_failed_does_not_swap(env: Env) -> None:
    """If every file to embed fails and nothing was removed, the live slot stays put."""
    slot, before = env.sidecar()[LOGICAL], _live(env)
    env.write("f2.md", _doc(2, "BOOM"))
    env.cycle(FakeEmbedder(fail_on="BOOM"))
    assert env.sidecar()[LOGICAL] == slot
    assert _live(env) == before


def test_copy_files_carries_only_the_requested_version(tmp_path: Path) -> None:
    """copy_files never carries a stale version of a file, even if the source holds one."""
    wrapper = ChromaWrapper(persist_directory=str(tmp_path / "c"))
    source = wrapper.create_collection("src")
    target = wrapper.create_collection("dst")
    vec = [1.0] + [0.0] * (DIM - 1)
    wrapper.add_documents(
        source,
        ids=["old0", "new0", "new1", "other0"],
        documents=["old", "new a", "new b", "other"],
        metadatas=[
            {"source_file": "/a", "content_hash": "h-old"},
            {"source_file": "/a", "content_hash": "h-new"},
            {"source_file": "/a", "content_hash": "h-new"},
            {"source_file": "/b", "content_hash": "h-b"},
        ],
        embeddings=[vec, vec, vec, vec],
    )
    copied = wrapper.copy_files(source, target, {"/a": "h-new"})
    assert copied == {"/a": 2}
    assert wrapper.file_versions(target) == {"/a": {"h-new": 2}}
    assert sorted(target.get()["ids"]) == ["new0", "new1"]


def test_stale_live_version_is_repaired(env: Env) -> None:
    """Live slot older than the checkpoint (an old agent killed mid-cycle): re-embed, don't carry.

    The pre-S-11 agent wrote the checkpoint before the swap, so a kill in
    between left the checkpoint on the new version and the live slot on the
    old one.  Carrying would find nothing to copy; the file must be repaired.
    """
    f1, f2, f3 = _paths(env)
    new_text = _doc(1, "edited while killed")
    env.write("f1.md", new_text)
    ckpt = json.loads(env.ckpt.read_text(encoding="utf-8"))
    ckpt["processed"][f1]["content_hash"] = content_hash(new_text)
    env.ckpt.write_text(json.dumps(ckpt), encoding="utf-8")

    env.write("f2.md", _doc(2, "edited"))  # any change triggers a cycle
    env.cycle()
    live = _live(env)
    assert set(live) == {f1, f2, f3}
    assert set(live[f1]) == {content_hash(new_text)}
