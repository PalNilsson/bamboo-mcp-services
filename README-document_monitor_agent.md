# document_monitor_agent

A production-oriented agent that watches one or more directories for new or changed documents, extracts and chunks text, computes deterministic chunk IDs, embeds chunks, and stores vectors and metadata in named ChromaDB collections.

---

## What it does

- Monitors a directory (including all subdirectories) for file changes via polling.
- Extracts text from `.pdf`, `.docx`, `.txt`, and `.md` files.
- Splits text into overlapping character chunks.
- Generates deterministic chunk IDs (stable across re-ingestion).
- Embeds chunks using a pluggable embedder (default: `sentence-transformers/all-MiniLM-L6-v2`).
- Stores vectors and metadata in a named ChromaDB collection (persistent local backend).
- Maintains a JSON checkpoint store to prevent re-processing unchanged files.
- Replaces stale vectors when file content changes.

---

## Design guarantees

### Deterministic IDs

Chunk IDs are derived from `absolute_file_path + chunk_index`, hashed with SHA-256, and prefixed with `doc:`. This ensures stable IDs across re-ingestion and replace-in-place behaviour when content changes.

### Zero-downtime updates — blue/green slot rotation

Every update cycle uses a **blue/green slot swap** so that concurrent readers (e.g. Bamboo MCP tools performing RAG queries) never observe an empty or partially-filled collection.

Two physical ChromaDB collections are maintained per logical collection name:

| Slot | Physical name |
|---|---|
| Blue | `<collection>__a` |
| Green | `<collection>__b` |

Only one slot is live at any time.  The other is the idle build target.  On each update cycle:

1. The idle slot is **deleted and recreated from scratch**.  This clears any embedding dimension locked in from a previous cycle — see [Dimension mismatch protection](#dimension-mismatch-protection) below.
2. New chunks are embedded and written into the idle slot.  Readers still address the live slot; they are unaffected.
3. A routing sidecar file (`<chroma-dir>/collection_routing.json`) is updated via `os.replace` — a POSIX atomic rename — to point the logical collection name at the newly-built slot.
4. The old live slot is deleted.

Between steps 2 and 4 the old slot remains fully queryable.  There is no window where the collection is empty or partially filled.

#### Routing sidecar

The file `<chroma-dir>/collection_routing.json` records the current live slot for each logical collection:

```json
{
  "atlas_docs": "atlas_docs__a"
}
```

It is written with a write-then-`os.replace` pattern, so it is never partially written.  **The sidecar is only written by `commit_swap()` after ingestion has completed successfully** — it is never written at agent startup.  This guarantees the sidecar always points at a slot that has been populated; a sidecar entry is not created until the first ingestion cycle completes.

**Routing invariant:** for every entry `L → P` in the sidecar, the physical collection `P` must exist in ChromaDB and contain at least one document.  The agent verifies this invariant at the end of every successful ingestion cycle and logs ERROR if it is violated.

#### Verifying the sidecar manually

```bash
# Uses $BAMBOO_CHROMA_PATH:
python scripts/verify_routing.py

# Explicit path:
python scripts/verify_routing.py --chroma-dir /data/.chromadb
```

Sample output:

```
[OK    ] atlas_docs            ->  atlas_docs__b            (324 docs)
[OK    ] bamboo_docs           ->  bamboo_docs__b           (74 docs)
[BROKEN] panda_docs            ->  panda_docs__a            (0 docs)

FAIL: 1 broken entrie(s):
  [BROKEN] panda_docs -> panda_docs__a  (0 docs)
```

Exit code 0 = all entries consistent; 1 = one or more broken entries.

#### Repairing a broken sidecar

If the sidecar is stale (pointing at an empty slot while data is in the other slot) the quickest repair is to re-run the document monitor with `--once` for the affected collection.  This triggers a fresh ingestion cycle which will write the sidecar to the newly populated slot.

If you need to repair manually (e.g. after discovering a discrepancy with `verify_routing.py`), edit `collection_routing.json` to point at the physical slot that actually contains documents, then re-run `verify_routing.py` to confirm.

A crash between the sidecar write and the deletion of the old slot leaves an extra collection on disk, which is deleted at the start of the next cycle.

#### Dimension mismatch protection

ChromaDB locks the embedding dimension of a collection on the first write.  If the embedder model ever changes (different model name, different version), the new vectors have a different dimension and ChromaDB rejects them.

With the blue/green design this can never corrupt live data:

- The idle slot is **always deleted and recreated** (`delete_collection` + `create_collection`) before any new vectors are written into it.  No dimension is inherited.
- If the write into the idle slot fails for any reason — including a dimension mismatch — the idle slot is cleaned up and the **live slot remains untouched and queryable**.
- The agent logs the error and retries on the next poll cycle.

### Replace-on-change strategy

When a file's content hash changes, the full blue/green cycle runs for that file: the idle slot is rebuilt with the new chunk set (which includes the updated file's chunks and all other files' chunks), and the swap promotes it atomically.  Stale vectors from the changed file never appear in query results.

---

## Installation & setup

Follow these steps in order. The `bamboo-document-monitor` command will not be available until all steps are complete.

### Step 1 — Install Miniforge

Miniforge is the recommended conda distribution. Do **not** use `brew install conda` — it installs a bare-bones version that won't set up your shell correctly.

```bash
brew install --cask miniforge
conda init zsh   # or 'conda init bash' if you use bash
```

Restart your terminal after running `conda init`. Alternatively, download the installer directly from [github.com/conda-forge/miniforge](https://github.com/conda-forge/miniforge).

### Step 2 — Create the conda environment

> Use Python 3.12 or earlier. Python 3.13+ is not yet reliably supported by ML libraries such as PyTorch and sentence-transformers.

**Apple Silicon:**
```bash
conda create -n bamboo-mcp-services python=3.12 -y
conda activate bamboo-mcp-services
conda install -c conda-forge -c pytorch pytorch cpuonly -y
```

**Intel macOS:**
```bash
conda create -n bamboo-mcp-services python=3.12 -y
conda activate bamboo-mcp-services
conda install -c pytorch -c conda-forge pytorch -y
```

PyTorch is installed via conda because it provides pre-compiled binaries tested for your platform, avoiding ABI and architecture issues. The remaining packages are installed with pip because they are not well-maintained on conda channels, but install cleanly once PyTorch is in place.

### Step 3 — Install remaining dependencies

```bash
pip install -r requirements.txt
pip install -e .
```

> **Known version constraints:** `torch==2.2.2` (the version available on macOS/miniforge with
> Python 3.12) was compiled against the NumPy 1.x ABI. Running it alongside NumPy 2.x produces
> `_ARRAY_API not found` errors at import time. The `requirements.txt` pins `numpy<2` to prevent
> this. If you see NumPy-related errors, run `pip install "numpy<2"`.

---

## Running the agent

### One-shot with multiple watch pairs (recommended)

Use `--watch DIR COLLECTION` (repeatable) to monitor several directories and
ingest each into its own named ChromaDB collection in one invocation.  This is
the standard deployment pattern when `bamboo-github-sync` writes to separate
per-collection output directories:

```bash
bamboo-document-monitor \
  --watch /data/bamboo/rag/panda_docs   panda_docs \
  --watch /data/bamboo/rag/atlas_docs   atlas_docs \
  --watch /data/bamboo/rag/bamboo_docs  bamboo_docs \
  --watch /data/bamboo/rag/rucio_docs   rucio_docs \
  --watch /data/bamboo/rag/root_docs    root_docs \
  --chroma-dir /data/bamboo/.chromadb \
  --once
```

Each `--watch` pair is processed sequentially.  A single embedder instance is
shared across all pairs to avoid loading the model multiple times.

Each pair gets its own checkpoint file, automatically named
`.document_monitor/checkpoints_<dir_name>_<collection>.json`, so file state is
tracked independently per directory.

### Single directory (simple case)

For a single directory, one `--watch` pair is sufficient:

```bash
bamboo-document-monitor \
  --watch /abs/path/to/docs panda_docs \
  --chroma-dir .chromadb \
  --once
```

### Long-running daemon

Omit `--once` to poll continuously, picking up new files as they arrive:

```bash
bamboo-document-monitor \
  --watch /data/bamboo/rag/panda_docs  panda_docs \
  --watch /data/bamboo/rag/bamboo_docs bamboo_docs \
  --chroma-dir .chromadb
```

Stop with Ctrl-C or SIGTERM.  In daemon mode, ticks are issued in round-robin
order across all watch pairs.

### Via module

```bash
python -m bamboo_mcp_services.agents.document_monitor_agent.cli \
  --watch /abs/path/to/docs panda_docs --chroma-dir .chromadb --once
```

> **First run on a new machine:** the agent loads the embedding model from local
> cache. On a fresh machine, trigger the download by running with
> `HF_HUB_OFFLINE=0`:
> ```bash
> HF_HUB_OFFLINE=0 bamboo-document-monitor \
>   --watch /abs/path/to/docs panda_docs --chroma-dir .chromadb --once
> ```

> **Always use absolute paths** for `--watch` directories and `--chroma-dir` to
> avoid the database being written to a different location depending on the
> working directory.

### Legacy flags (deprecated)

`--dir` and `--collection` still work as a backward-compatible single-pair
shorthand and emit a `DeprecationWarning`:

```bash
# Deprecated — use --watch instead
bamboo-document-monitor --dir ./documents --collection atlas_docs --once
```

---

## Multiple corpora

Each `--watch DIR COLLECTION` pair maps a normalised output directory to a
logical ChromaDB collection.  Multiple repos can share the same collection
(and therefore the same directory) — the document-monitor has no concept of
per-repo boundaries, only per-directory ones.

Verify collection names and chunk counts at any time:

```bash
python -c "
import chromadb
client = chromadb.PersistentClient(path='/data/bamboo/.chromadb')
for col in client.list_collections():
    print(col.name, '  count:', col.count())
"
```

> **Note:** collections are stored under slotted names (e.g. `panda_docs__a`
> or `panda_docs__b`).  The active slot for each logical name is recorded in
> `<chroma-dir>/collection_routing.json`.  You will normally see exactly one
> slotted collection per logical name; a second slot may briefly appear during
> an active update cycle.

---

## Re-ingestion

Re-ingestion is required when:

- You change `--chunk-size` or `--chunk-overlap` (existing chunks remain at the old size until wiped).
- The ChromaDB index becomes corrupted or out of sync with the SQLite metadata.
- You want to start fresh after adding or removing documents.
- The agent previously ran with `DummyEmbedder` (zero vectors) — see [Embedding troubleshooting](#embedding-troubleshooting).
- You are upgrading from a version of the agent that predates the blue/green routing (collections named `atlas_docs` rather than `atlas_docs__a` / `atlas_docs__b`) — see note below.

To re-ingest cleanly:

```bash
# 1. Wipe the vector store, routing sidecar, and checkpoint
rm -rf .chromadb .document_monitor/checkpoints.json

# 2. Re-run the agent — it will process all files from scratch into slot __a
bamboo-document-monitor \
  --watch /abs/path/to/docs my_collection \
  --chroma-dir /abs/path/to/.chromadb \
  --once
```

> **Upgrading from a pre-blue/green version:** the old agent wrote vectors into
> a collection named exactly `<collection>` (e.g. `atlas_docs`).  The new agent
> uses `atlas_docs__a` / `atlas_docs__b` and will not find or use the old
> collection.  After wiping and re-ingesting, the old `atlas_docs` collection
> can be deleted manually:
> ```python
> import chromadb
> client = chromadb.PersistentClient(path=".chromadb")
> client.delete_collection("atlas_docs")   # remove the legacy unslotted collection
> ```

---

## Embedding troubleshooting

The agent logs a warning and falls back to `DummyEmbedder` (zero vectors) if
the embedding stack is not correctly installed:

```
WARNING ... Local HF instantiation failed (will try hub or dummy): ...
WARNING ... Falling back to DummyEmbedder for embeddings (no HF available).
```

**This is a silent data corruption issue** — the agent completes successfully,
files are marked as ingested in the checkpoint, and ChromaDB is populated, but
all vectors are zero.  Similarity search will return garbage results.  Any
ChromaDB data ingested while `DummyEmbedder` was active must be deleted and
re-ingested after fixing the embedding stack.

Verify the embedding stack is working before ingesting:

```bash
python -c "
from langchain_huggingface import HuggingFaceEmbeddings
e = HuggingFaceEmbeddings(model_name='all-MiniLM-L6-v2')
print('dims:', len(e.embed_documents(['test'])[0]))
"
```

Expected output: `dims: 384`. If you see an error, install the missing packages:

```bash
pip install -r requirements.txt
```

The `requirements.txt` pins `torch==2.2.2`, `transformers==4.40.0`, and
`sentence-transformers==2.7.0` as a tested combination for macOS/miniforge
with Python 3.12. Installing newer versions of these packages without also
upgrading PyTorch will produce import errors.

---

## Starting a new session

Once set up, you only need to activate the environment at the start of each session:

```bash
conda activate bamboo-mcp-services
```

To verify everything is in order:

```bash
conda info
python --version
```

If a virtualenv is currently active, deactivate it first — only one environment manager should be active at a time:

```bash
deactivate
conda activate bamboo-mcp-services
```

---

## Configuration options

> **Chunk size guidance:** the default of 3000 characters is a good balance for technical
> documentation with long class or function definitions. If your documents are short
> (e.g. individual wiki pages), a smaller value like 1000–1500 may give better retrieval
> precision. Changing this setting requires a full re-ingestion — see [Re-ingestion](#re-ingestion).

| Option | Default | Description |
|---|---|---|
| `--dir` | *(required)* | Directory to monitor (all subdirectories are included) |
| `--collection` | `atlas_docs` | ChromaDB collection name. Use a distinct name per corpus to avoid mixing document sets. |
| `--poll-interval` | `10` | Poll interval in seconds (daemon mode only) |
| `--chroma-dir` | `.chromadb` | ChromaDB persistence directory |
| `--checkpoint-file` | `.document_monitor/checkpoints.json` | JSON checkpoint path. Use a distinct path per corpus when running multiple instances. |
| `--chunk-size` | `3000` | Characters per chunk |
| `--chunk-overlap` | `300` | Overlap between chunks |
| `--once` | off | Run a single poll cycle then exit |

---

## Checkpoint format

```json
{
  "processed": {
    "/abs/path/to/file.pdf": {
      "content_hash": "sha256...",
      "processed_ts": "2026-03-12T12:34:56Z",
      "chunks": 5,
      "chunk_ids": ["doc:...", "doc:..."]
    }
  }
}
```

---

## CI and testing

Use a dummy embedder in tests to avoid model downloads:

```python
class DummyEmbedder:
    def encode(self, texts, show_progress_bar=False):
        return [[0.0] * 8 for _ in texts]
```
