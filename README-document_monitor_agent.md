# document_monitor_agent

A production-oriented agent that watches a directory for new or changed documents, extracts and chunks text, computes deterministic chunk IDs, embeds chunks, and stores vectors and metadata in a local ChromaDB collection.

---

## What it does

- Monitors a directory (non-recursive) for file changes via polling.
- Extracts text from `.pdf`, `.docx`, `.txt`, and `.md` files.
- Splits text into overlapping character chunks.
- Generates deterministic chunk IDs (stable across re-ingestion).
- Embeds chunks using a pluggable embedder (default: `sentence-transformers`).
- Stores vectors and metadata in ChromaDB (`duckdb+parquet` backend).
- Maintains a JSON checkpoint store to prevent re-processing unchanged files.
- Replaces stale vectors when file content changes.

---

## Design guarantees

### Deterministic IDs

Chunk IDs are derived from `absolute_file_path + chunk_index`, hashed with SHA-256, and prefixed with `doc:`. This ensures stable IDs across re-ingestion and replace-in-place behaviour when content changes.

### Replace-on-change strategy

When a file's content hash changes:

1. Previous chunk IDs (stored in checkpoint) are deleted from ChromaDB.
2. New chunks and embeddings are computed.
3. New vectors are inserted under the same stable ID scheme.
4. Checkpoint is updated.

This prevents stale vectors from being retrieved by RAG and reduces hallucination risk.

---

## Installation

Add the following to `requirements.txt`:

```
chromadb>=0.4.0
sentence-transformers>=2.2.2
pdfminer.six>=20221105
python-docx>=0.8.11
```

Then install:

```bash
pip install -r requirements.txt
pip install -e .
```

> **Note:** Conda is strongly recommended over pip for this agent. See [Environment Setup](#environment-setup) below.

---

## Running the agent

```bash
askpanda-document-monitor-agent --dir ./documents --poll-interval 10 --chroma-dir .chromadb
```

Or via module:

```bash
python -m askpanda_atlas_agents.agents.document_monitor_agent.cli --dir ./documents
```

---

## Configuration options

| Option | Default | Description |
|---|---|---|
| `--dir` | *(required)* | Directory to monitor |
| `--poll-interval` | `10` | Poll interval in seconds |
| `--chroma-dir` | `.chromadb` | ChromaDB persistence directory |
| `--checkpoint-file` | `.document_monitor/checkpoints.json` | JSON checkpoint path |
| `--chunk-size` | `1000` | Characters per chunk |
| `--chunk-overlap` | `200` | Overlap between chunks |

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

---

## Environment setup

This agent depends on ML libraries (`sentence-transformers`, `torch`, `numpy`, etc.) that contain compiled native extensions and can cause dependency conflicts when installed with pip alone. **Conda is strongly recommended** as it distributes pre-compiled, mutually compatible binaries.

### Installing Miniforge

Miniforge is the recommended conda distribution. Do **not** use `brew install conda` — it installs a bare-bones version that won't set up your shell correctly.

Instead, install the Miniforge cask and initialise your shell:

```bash
brew install --cask miniforge
conda init zsh   # or 'conda init bash' if you use bash
```

Then restart your terminal. Alternatively, download the installer directly from [github.com/conda-forge/miniforge](https://github.com/conda-forge/miniforge).

### Apple Silicon (M1/M2/M3)

```bash
conda create -n askpanda python=3.10 -y
conda activate askpanda
conda install -c conda-forge -c pytorch pytorch cpuonly -y
pip install sentence-transformers langchain langchain-community chromadb pdfminer.six python-docx
```

### Intel macOS

```bash
conda create -n askpanda python=3.10 -y
conda activate askpanda
conda install -c pytorch -c conda-forge pytorch -y
pip install sentence-transformers langchain langchain-community chromadb pdfminer.six python-docx
```

### Switching from a virtualenv

Only one environment manager should be active at a time. If a virtualenv is currently active, deactivate it first:

```bash
deactivate
conda activate askpanda
```

Your virtualenv remains on disk and can be reactivated at any time.
