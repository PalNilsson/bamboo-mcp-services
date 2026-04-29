# Bamboo MCP Services

**Bamboo MCP Services** is a collection of cooperative, Python-based services that feed data into the **Bamboo Toolkit**, supporting the ATLAS Experiment at CERN.

> ⚠️ **Early development**
> This repository is under active development. The `document-monitor`, `ingestion`, and `cric` services are ready for use. Other agents are planned.

---

## Current status

| Agent | Status |
|---|---|
| `document-monitor-agent` | ✅ Ready |
| `ingestion-agent` | ✅ Ready |
| `cric-agent` | ✅ Ready |
| `github-doc-sync-agent` | ✅ Ready |
| `dast-agent` | 📋 Planned |
| `supervisor-agent` | 📋 Planned |
| `index-builder-agent` | 📋 Planned |
| `feedback-agent` | 📋 Planned |
| `metrics-agent` | 📋 Planned |

---

## Getting started

### Install

This project uses a conda environment.  If you have not set it up yet:

```bash
conda create -n bamboo-mcp-services python=3.12
conda activate bamboo-mcp-services
pip install -r requirements.txt
pip install -e ".[dev]"
```

On a normal working day, just activate the existing environment:

```bash
conda activate bamboo-mcp-services
pip install -e .   # pick up any dependency or version changes
```

> **Note:** The project uses a `src/` layout, so the package must be installed
> (with `-e`) before running tests or CLI tools.  See
> [Common pitfalls](#common-pitfalls) if commands are not found or imports fail.

For development (includes pytest and flake8):

```bash
pip install -e ".[dev]"
```

### Run the document monitor agent

```bash
# Process all files once and exit:
bamboo-document-monitor --dir ./documents --chroma-dir .chromadb --once

# Run as a long-lived daemon (polls every 10 seconds):
bamboo-document-monitor --dir ./documents --poll-interval 10 --chroma-dir .chromadb
```

Full documentation: [README-document_monitor_agent.md](./README-document_monitor_agent.md)

### Run the ingestion agent

```bash
# Download all queues once and exit:
bamboo-ingestion --config src/bamboo_mcp_services/resources/config/ingestion-agent.yaml --once

# Run as a long-lived daemon (polls every 30 minutes):
bamboo-ingestion --config src/bamboo_mcp_services/resources/config/ingestion-agent.yaml

# Inspect what was collected:
python scripts/dump_ingestion_db.py --count
python scripts/dump_ingestion_db.py --table jobs --queue SWT2_CPB --limit 5
```

Full documentation: [README-ingestion_agent.md](./README-ingestion_agent.md)

### Run the CRIC agent

```bash
# Load CRIC queuedata once and exit:
bamboo-cric --data cric.db --once

# Run as a long-lived daemon (re-reads file every 10 minutes):
bamboo-cric --data cric.db

# Inspect what was loaded:
duckdb cric.db "SELECT COUNT(*) FROM queuedata"
duckdb cric.db "SELECT queue, status, cloud, tier FROM queuedata LIMIT 10"
```

Full documentation: [README-cric_agent.md](./README-cric_agent.md)

### Run the GitHub documentation sync agent

```bash
# Sync all configured repositories once and exit:
bamboo-github-sync --config src/bamboo_mcp_services/resources/config/github-doc-sync-agent.yaml --once

# Run as a long-lived daemon (checks for new commits every hour):
bamboo-github-sync --config src/bamboo_mcp_services/resources/config/github-doc-sync-agent.yaml

# Authenticate to raise the GitHub API rate limit (required for private repos):
export GITHUB_TOKEN=ghp_your_token_here
bamboo-github-sync --config repos.yaml --once
```

Full documentation: [README-github_doc_sync_agent.md](./README-github_doc_sync_agent.md)

---

## Agents

### `document-monitor-agent` ✅ Ready

Watches a directory (including all subdirectories) for new or changed documents and ingests them into ChromaDB for use in RAG pipelines. Extracts and chunks text from `.pdf`, `.docx`, `.txt`, and `.md` files, computes deterministic chunk IDs, and stores vectors and metadata locally.

→ [Full documentation](./README-document_monitor_agent.md)

### `ingestion-agent` ✅ Ready

Periodically downloads job metadata from [BigPanda](https://bigpanda.cern.ch) for a configured list of ATLAS computing queues and persists the data in a local [DuckDB](https://duckdb.org) database for downstream use by Bamboo. Stores per-job records, facet summaries, and error frequency tables. Supports one-shot and long-running daemon modes.

Key features:
- Configurable queue list, poll cycle (default: 30 min), and inter-queue delay
- Bulk DataFrame inserts — handles 10k+ jobs per queue in under 2 seconds
- Rotating log file, `--log-level DEBUG` support, clean Ctrl-C / SIGTERM shutdown
- `scripts/dump_ingestion_db.py` for inspecting the database from the command line

→ [Full documentation](./README-ingestion_agent.md)

### `cric-agent` ✅ Ready

Periodically reads ATLAS queue metadata from the CRIC Computing Resource
Information Catalogue (via CVMFS) and stores the latest snapshot in a local
[DuckDB](https://duckdb.org) database. Uses SHA-256 content hashing to skip
database writes when the source file has not changed since the last cycle,
and performs a full table replace on each changed load so the database stays
small regardless of how long the agent runs.

Key features:
- Single `queuedata` table — one row per ATLAS computing queue, ~90 columns
- Full data dictionary in `schema_annotations.py` for use in LLM prompts
- 10-minute poll interval with hash-based skip when CVMFS content is unchanged
- `--data PATH` required CLI flag keeps the DB path out of the config file
- Rotating log file, `--log-level DEBUG` support, clean Ctrl-C / SIGTERM shutdown

→ [Full documentation](./README-cric_agent.md)

### `github-doc-sync-agent` ✅ Ready

Periodically polls one or more GitHub repositories (including GitHub wikis),
downloads changed `.md` and `.rst` documentation files, and writes normalised
Markdown to a local directory for RAG ingestion.  Uses the GitHub REST API with
commit SHA caching for regular repos, and `git clone --depth 1` for wiki repos
(which are not accessible via the REST API).  Unchanged repositories are skipped
with a single API call or clone.

The agent is a **file writer only**.  It is designed to feed the
`document-monitor-agent`, which handles chunking, embedding, and ChromaDB
insertion.  The two agents are decoupled and can run independently.

Key features:
- Multi-repository support via a YAML config file; per-repo branch, glob
  filters, and `within_hours` recency check
- GitHub wiki support via `wiki: true` config flag
- SHA-based incremental sync — full download only when new commits are detected
- RST → Markdown conversion and YAML frontmatter injection for RAG-ready output
- Per-repo failure isolation — one failing repository never aborts the others
- `GITHUB_TOKEN` support to raise the API rate limit from 60 to 5,000 req/hour

→ [Full documentation](./README-github_doc_sync_agent.md)

Will extract DAST help-list email threads (e.g. via Outlook), convert them into structured JSON, and run a daily digest pass producing cleaned Q/A pairs, thread summaries, tags, and resolution status. Output feeds RAG corpora and optional fine-tuning datasets.

### `supervisor-agent` 📋 Planned

Will act as a control plane — ensuring required agents and services are running, restarting agents on failure, enforcing schedules, and providing a single entry point to bring up the full system.

### `index-builder-agent` 📋 Planned

Will build embedding indices for plugin corpora from sources including DAST digests, documentation, and curated knowledge. May be superseded by the `document-monitor-agent`.

### `feedback-agent` 📋 Planned

Will capture user feedback from Bamboo (e.g. *helpful / not helpful*) and store it in structured form for later analysis.

### `metrics-agent` 📋 Planned

Will collect structured metrics from Bamboo and agents (latency, tool usage, failures) and export them to JSON and optionally Grafana/Prometheus-compatible backends.

---

## Agent lifecycle interface

All agents follow a minimal, consistent lifecycle interface to simplify supervision, testing, and orchestration:

```python
class Agent:
    def start(self) -> None:
        """Initialize resources and enter running state."""

    def tick(self) -> None:
        """Execute one scheduled unit of work (poll, sync, digest, etc.)."""

    def health(self) -> dict:
        """Return lightweight health/status information."""

    def stop(self) -> None:
        """Gracefully release resources and shut down."""
```

Long-running agents run a scheduler loop calling `tick()`. Batch agents may run `start() → tick() → stop()` once. The `supervisor-agent` will interact only through this interface.

A minimal no-op `dummy-agent` is included as a template and for validating the lifecycle:

```bash
bamboo-dummy --tick-interval 1.0
```

Stop with Ctrl+C or SIGTERM. When adding a new agent, register its entry point in `pyproject.toml` under `[project.scripts]`.

---

## Repository layout

```
bamboo-mcp-services/
├─ README.md
├─ CHANGELOG.md
├─ README-document_monitor_agent.md
├─ README-ingestion_agent.md
├─ README-cric_agent.md
├─ README-github_doc_sync_agent.md
├─ pyproject.toml
├─ requirements.txt
├─ scripts/
│  ├─ dump_ingestion_db.py       # inspect the ingestion database from the CLI
│  └─ bump_version.py            # bump the version string across all files
├─ src/
│  └─ bamboo_mcp_services/
│     ├─ common/
│     │  ├─ cli.py                   # shared startup banner helper
│     │  └─ storage/
│     │     ├─ duckdb_store.py       # low-level DuckDB helpers
│     │     ├─ schema.py             # DDL — single source of truth for jobs tables
│     │     └─ schema_annotations.py # field descriptions for LLM context (jobs + queuedata)
│     ├─ agents/
│     │  ├─ base.py                  # Agent lifecycle interface
│     │  ├─ ingestion_agent/
│     │  │  ├─ agent.py
│     │  │  ├─ bigpanda_jobs_fetcher.py
│     │  │  └─ cli.py
│     │  ├─ cric_agent/
│     │  │  ├─ agent.py
│     │  │  ├─ cric_fetcher.py
│     │  │  └─ cli.py
│     │  ├─ github_doc_sync_agent/
│     │  │  ├─ agent.py
│     │  │  ├─ github_doc_syncer.py
│     │  │  ├─ github_markdown_sync.py  # vendored from github-documentation-sync
│     │  │  └─ cli.py
│     │  ├─ document_monitor_agent/
│     │  ├─ dummy_agent/
│     │  ├─ dast_agent/              # planned
│     │  ├─ supervisor_agent/        # planned
│     │  ├─ index_builder_agent/     # planned
│     │  ├─ feedback_agent/          # planned
│     │  └─ metrics_agent/           # planned
│     ├─ plugin/                     # Bamboo MCP plugin adapter
│     └─ resources/
│        └─ config/
│           ├─ ingestion-agent.yaml
│           ├─ cric-agent.yaml
│           └─ github-doc-sync-agent.yaml
├─ tests/
│  └─ agents/
│     ├─ ingestion_agent/
│     ├─ cric_agent/
│     ├─ github_doc_sync_agent/
│     ├─ dummy_agent/
│     └─ test_base_agent.py
└─ .github/
   └─ workflows/
      └─ ci.yml
```

---

## Shared tooling

Agents draw on shared components in `common/`:

- **CLI utilities** — `common/cli.py` provides `log_startup_banner()`, called by every agent on startup to emit a consistent `prog  version=X.Y.Z  python=A.B.C` log line
- **Storage** — DuckDB store, typed schema DDL (`schema.py`), field annotations for LLM context (`schema_annotations.py`)
- **Vector stores** — ChromaDB, embedding adapters
- **PanDA / BigPanDA** — metadata fetching, snapshot downloads
- **Email** — local Microsoft Outlook access, thread reconstruction and parsing
- **Metrics** — structured event schemas, JSON and Grafana-compatible exporters

---

## Development

### Running tests

```bash
pytest
pytest --cov=bamboo_mcp_services --cov-report=term-missing
```

### Linting

```bash
flake8 src tests
pylint src/bamboo_mcp_services
```

### Common pitfalls

**`ModuleNotFoundError: bamboo_mcp_services`** — run `pip install -e .` from the
repository root (where `pyproject.toml` lives).

**Editable install fails** — confirm that `src/bamboo_mcp_services/` exists and
contains an `__init__.py`.

**Agent logs wrong version after `bump_version.py`** — `importlib.metadata` reads
the version baked in at install time. Run `pip install -e .` after every bump.

**Code changes have no effect at runtime** — if `pip install .` (without `-e`) was
ever run, a non-editable copy in `site-packages` will shadow the source tree.
Fix with:
```bash
pip uninstall bamboo-mcp-services -y
pip install -e .
```
Verify the right file is being imported with:
```bash
python -c "import bamboo_mcp_services.agents.github_doc_sync_agent.github_markdown_sync as m; print(m.__file__)"
```
The path should point into your development tree, not `site-packages`.

**`document-monitor-agent` logs `Falling back to DummyEmbedder`** — the embedding
stack (`torch`, `sentence-transformers`, `langchain-huggingface`) is not installed
or has a version conflict.  Install via `pip install -r requirements.txt` and verify
with:
```bash
python -c "
from langchain_huggingface import HuggingFaceEmbeddings
e = HuggingFaceEmbeddings(model_name='all-MiniLM-L6-v2')
print('dims:', len(e.embed_documents(['test'])[0]))
"
```
Expected output: `dims: 384`.  If you see a PyTorch or NumPy version error, see
the embedding stack constraints in `pyproject.toml` and `requirements.txt`.
The `DummyEmbedder` produces zero vectors — any ChromaDB data ingested while it
was active must be deleted and re-ingested with real embeddings.

**PyTorch/NumPy version conflict** — `torch==2.2.2` (the version available on
macOS/miniforge with Python 3.12) was compiled against the NumPy 1.x ABI.
Running it alongside NumPy 2.x produces `_ARRAY_API not found` errors.
Fix with `pip install "numpy<2"`.

---

## Continuous integration

GitHub Actions runs linting (`pylint`, `flake8`) and the full unit test suite
(`pytest`) on every push. All agents and shared tools must have corresponding
unit tests.

---

## Relationship to Bamboo

The `plugin/` package provides the integration layer between Bamboo MCP Services
and the Bamboo Toolkit, keeping service logic independent of the UI and
orchestration layer.

---

## Contributing

Design feedback and contributions are welcome. This repository currently represents
an architectural blueprint guiding development — interfaces are intended to be
stable, but implementations will evolve.

### Repository setup

The canonical repository is at **https://github.com/BNLNPPS/bamboo-mcp-services**.
Development follows a standard fork-and-pull-request workflow.

First-time setup:

```bash
# Clone your fork
git clone https://github.com/<your-username>/bamboo-mcp-services.git
cd bamboo-mcp-services

# Add the canonical repo as upstream
git remote add upstream https://github.com/BNLNPPS/bamboo-mcp-services.git

# Verify
git remote -v
# origin    https://github.com/<your-username>/bamboo-mcp-services.git (fetch)
# origin    https://github.com/<your-username>/bamboo-mcp-services.git (push)
# upstream  https://github.com/BNLNPPS/bamboo-mcp-services.git (fetch)
# upstream  https://github.com/BNLNPPS/bamboo-mcp-services.git (push)
```

Day-to-day workflow:

```bash
# Push your changes to your fork
git push origin master

# Open a pull request from your fork to BNLNPPS/bamboo-mcp-services via GitHub

# Keep your fork in sync with upstream
git fetch upstream
git merge upstream/master
```
