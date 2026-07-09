# Bamboo MCP Services

**Bamboo MCP Services** is a collection of cooperative, Python-based services that feed data into the **Bamboo Toolkit**, supporting the ATLAS Experiment at CERN.

> ⚠️ **Early development**
> This repository is under active development. The `document-monitor`, `ingestion`, `cric`, `github-doc-sync`, `supervisor`, and `dashboard` scripts are ready for use. Other scripts are planned.

---

## Current status

| Script | Status |
|---|---|
| `document-monitor` | ✅ Ready |
| `ingestion` | ✅ Ready |
| `cric` | ✅ Ready |
| `github-doc-sync` | ✅ Ready |
| `supervisor` | ✅ Ready |
| `dashboard` | ✅ Ready |
| `dast` | 📋 Planned |
| `index-builder` | 📋 Planned |
| `feedback` | 📋 Planned |
| `metrics` | 📋 Planned |

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

### Quick start: RAG only (`document-monitor`)

If you just want to test the RAG (retrieval-augmented generation) pipeline, the
only script you need is `document-monitor`. It watches a folder of documents and
ingests them into a ChromaDB collection you can query — everything else in this
repo (ingestion, CRIC, GitHub sync, the dashboard, the supervisor) is optional
and can be added later.

```bash
# Process a folder of documents once and exit:
bamboo-document-monitor \
  --watch /data/bamboo/rag/panda_docs panda_docs \
  --chroma-dir /data/bamboo/.chromadb --once

# Or run continuously, watching for new/changed files (polls every 10 seconds):
bamboo-document-monitor \
  --watch /data/bamboo/rag/panda_docs panda_docs \
  --chroma-dir /data/bamboo/.chromadb
```

- `--watch DIR COLLECTION` is repeatable — add one pair per document source you
  want kept in its own ChromaDB collection.
- `DIR` is created automatically if it doesn't exist. Drop `.pdf`, `.docx`,
  `.txt`, or `.md` files into it and they'll be chunked, embedded, and written
  to `COLLECTION`.
- First run on a new machine needs network access once, to download the
  embedding model. After that it runs fully offline from the cached model.

Full documentation: [README-document_monitor_agent.md](./README-document_monitor_agent.md)

### Advanced: run the full system

Everything below — job ingestion, CRIC queue metadata, GitHub doc sync, the
dashboard, and the supervisor that runs them all together — is only needed
once you're past RAG-only testing and want the full Bamboo MCP Services system.

#### Run everything with the supervisor (recommended)

The supervisor is the easiest way to run the full system.  It starts every script,
monitors daemons for unexpected exits, and dispatches scheduled jobs — no manual
juggling of individual processes needed.

```bash
# Copy and edit the bundled config (first time only)
cp src/bamboo_mcp_services/resources/config/supervisor-agent.yaml ./supervisor-agent.yaml
$EDITOR supervisor-agent.yaml   # adjust paths, enable/disable scripts as needed

# Start everything
bamboo-supervisor --config supervisor-agent.yaml

# Verify the config without starting anything
bamboo-supervisor --config supervisor-agent.yaml --status
```

Stop with **Ctrl-C** or `kill -TERM <pid>`.

Full documentation: [README-supervisor-agent.md](./README-supervisor-agent.md)

#### Run the ingestion script

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

#### Run the CRIC script

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

#### Run the GitHub documentation sync script

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

#### Run the dashboard script

```bash
# Serve the monitoring dashboard (reads jobs.duckdb and cric.duckdb by default):
bamboo-dashboard

# Custom database paths and port:
bamboo-dashboard --jobs-db /data/jobs.duckdb --cric-db /data/cric.duckdb --port 9090

# Start, verify the server is up, print the URL, then exit:
bamboo-dashboard --once
```

Open `http://localhost:8080` in any browser to view live job metrics, queue status, and error summaries.

Full documentation: [README-dashboard_agent.md](./README-dashboard_agent.md)

## Scripts

### `document-monitor` ✅ Ready

Watches one or more directories for new or changed documents and ingests each into a named ChromaDB collection for use in RAG pipelines. Each `--watch DIR COLLECTION` pair (repeatable) maps a normalised output directory to a logical collection, allowing different document sources to be kept in separate collections. Extracts and chunks text from `.pdf`, `.docx`, `.txt`, and `.md` files, computes deterministic chunk IDs, and stores vectors and metadata locally.

Updates are performed using a **blue/green slot rotation**: vectors are written into an idle ChromaDB collection while the live collection remains fully queryable, then the routing sidecar is updated atomically via `os.replace` to promote the new slot. This means there is no window where the collection is empty or partially filled, regardless of how long embedding takes. The idle slot is always deleted and recreated from scratch before each build, which also eliminates ChromaDB dimension-mismatch errors when the embedder model changes.

Key features:
- Repeatable `--watch DIR COLLECTION` for multi-corpus ingestion in one invocation
- Blue/green slot rotation — zero reader downtime during updates
- `--model-path` for air-gapped machines (fatal on load failure, no silent `DummyEmbedder` fallback)
- `--log-file PATH` — rotating log file (10 MB / 5 backups) alongside stderr
- `--once` for one-shot runs; daemon mode with Ctrl-C / SIGTERM shutdown

→ [Full documentation](./README-document_monitor_agent.md)

### `ingestion` ✅ Ready

Periodically downloads job metadata from [BigPanda](https://bigpanda.cern.ch) for a configured list of ATLAS computing queues and persists the data in a local [DuckDB](https://duckdb.org) database for downstream use by Bamboo. Stores per-job records, facet summaries, and error frequency tables. Supports one-shot and long-running daemon modes.

Key features:
- Configurable queue list, poll cycle (default: 30 min), and inter-queue delay
- Bulk DataFrame inserts — handles 10k+ jobs per queue in under 2 seconds
- Rotating log file, `--log-level DEBUG` support, clean Ctrl-C / SIGTERM shutdown
- `scripts/dump_ingestion_db.py` for inspecting the database from the command line

→ [Full documentation](./README-ingestion_agent.md)

### `cric` ✅ Ready

Periodically reads ATLAS queue metadata from the CRIC Computing Resource
Information Catalogue (via CVMFS) and stores the latest snapshot in a local
[DuckDB](https://duckdb.org) database. Uses SHA-256 content hashing to skip
database writes when the source file has not changed since the last cycle,
and performs a full table replace on each changed load so the database stays
small regardless of how long the script runs.

Table replacements use a **shadow-rename swap**: the new data is written into
`queuedata_staging` first, then a short single-transaction `ALTER TABLE RENAME`
promotes it to `queuedata` atomically. Readers never see a gap between the old
and new snapshots.

Key features:
- Single `queuedata` table — one row per ATLAS computing queue, ~90 columns
- Full data dictionary in `schema_annotations.py` for use in LLM prompts
- 10-minute poll interval with hash-based skip when CVMFS content is unchanged
- `--data PATH` required CLI flag keeps the DB path out of the config file
- Rotating log file, `--log-level DEBUG` support, clean Ctrl-C / SIGTERM shutdown

→ [Full documentation](./README-cric_agent.md)

### `github-doc-sync` ✅ Ready

Periodically polls one or more GitHub repositories (including GitHub wikis),
downloads changed `.md` and `.rst` documentation files, and writes normalised
Markdown to a local directory for RAG ingestion.  Uses the GitHub REST API with
commit SHA caching for regular repos, and `git clone --depth 1` for wiki repos
(which are not accessible via the REST API).  Unchanged repositories are skipped
with a single API call or clone.

This script is a **file writer only**.  It is designed to feed
`document-monitor`, which handles chunking, embedding, and ChromaDB
insertion.  The two are decoupled and can run independently.

Key features:
- Multi-repository support via a YAML config file; per-repo `collection`,
  branch, glob filters, and `within_hours` recency check — `collection`
  declares which ChromaDB collection a repo's normalised output belongs to
- GitHub wiki support via `wiki: true` config flag
- SHA-based incremental sync — full download only when new commits are detected
- RST → Markdown conversion and YAML frontmatter injection for RAG-ready output
- Per-repo failure isolation — one failing repository never aborts the others
- `GITHUB_TOKEN` support to raise the API rate limit from 60 to 5,000 req/hour

→ [Full documentation](./README-github_doc_sync_agent.md)

### `supervisor` ✅ Ready

Acts as the control plane for the full Bamboo MCP Services system.  Starts all
other scripts as child subprocesses, monitors long-running (daemon) scripts and
restarts them on failure with exponential back-off, and dispatches
short-lived (scheduled) scripts on a configurable interval.  Provides a single
`bamboo-supervisor` command to bring up the entire system.

Key features:
- Mixed daemon/scheduled mode: each script independently configured as a
  long-running daemon or a periodic one-shot
- Exponential back-off on rapid restarts (5 s → 300 s cap)
- Dependency ordering via `depends_on_file` — ensures `cric.duckdb` exists
  before the ingestion script starts
- `--status` flag prints a JSON config summary without starting any processes
- Clean SIGTERM propagation and configurable `stop_timeout_s` before SIGKILL
- Extensible health reporting (HTTP endpoint planned for remote deployments)

→ [Full documentation](./README-supervisor-agent.md)

### `dashboard` ✅ Ready

Serves a live, dark-themed single-page web UI for monitoring the Bamboo MCP Services system.  Starts a [FastAPI](https://fastapi.tiangolo.com/) / [uvicorn](https://www.uvicorn.org/) HTTP server in a background daemon thread and exposes REST endpoints for job metrics, queue status, and error summaries.  The dashboard auto-refreshes on a configurable interval and queries the DuckDB databases written by the other scripts — no additional processing is performed.

Key features:
- Read-only DuckDB access — safe to run alongside writing scripts (MVCC snapshot isolation)
- Dark-themed single-page dashboard with Chart.js doughnut and bar charts
- Panels for job status breakdown, top queues by count, CRIC queue status, cloud distribution, and top 25 error codes
- Configurable bind address, port, and auto-refresh interval
- `--once` flag for scripted startup verification
- Rotating log file, `--log-level DEBUG` support, clean Ctrl-C / SIGTERM shutdown

→ [Full documentation](./README-dashboard_agent.md)

### `dast` 📋 Planned

Will extract DAST help-list email threads (e.g. via Outlook), convert them into structured JSON, and run a daily digest pass producing cleaned Q/A pairs, thread summaries, tags, and resolution status. Output feeds RAG corpora and optional fine-tuning datasets.

### `index-builder` 📋 Planned

Will build embedding indices for plugin corpora from sources including DAST digests, documentation, and curated knowledge. May be superseded by `document-monitor`.

### `feedback` 📋 Planned

Will capture user feedback from Bamboo (e.g. *helpful / not helpful*) and store it in structured form for later analysis.

### `metrics` 📋 Planned

Will collect structured metrics from Bamboo and the other scripts (latency, tool usage, failures) and export them to JSON and optionally Grafana/Prometheus-compatible backends.

---

## Script lifecycle interface

Internally, every script above is built on top of an `Agent` base class (see
`agents/base.py`) that follows a minimal, consistent lifecycle interface to
simplify supervision, testing, and orchestration:

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

Long-running scripts run a scheduler loop calling `tick()`. Batch scripts may run `start() → tick() → stop()` once. `supervisor` interacts with child processes through their CLI entry points rather than this interface directly, but follows the same lifecycle itself.

A minimal no-op script, `dummy`, is included as a template and for validating the lifecycle:

```bash
bamboo-dummy --tick-interval 1.0
```

Stop with Ctrl+C or SIGTERM. When adding a new script, register its entry point in `pyproject.toml` under `[project.scripts]`.

---

## Repository layout

```
bamboo-mcp-services/
├─ README.md
├─ README-supervisor-agent.md
├─ README-document_monitor_agent.md
├─ README-ingestion_agent.md
├─ README-cric_agent.md
├─ README-github_doc_sync_agent.md
├─ README-dashboard_agent.md
├─ CHANGELOG.md
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
│     │  ├─ supervisor_agent/
│     │  │  ├─ __init__.py
│     │  │  ├─ agent.py               # SupervisorAgent
│     │  │  ├─ scheduler.py           # per-agent scheduling state
│     │  │  └─ cli.py                 # bamboo-supervisor entry point
│     │  ├─ dashboard_agent/
│     │  │  ├─ agent.py               # DashboardAgent, DashboardConfig, FastAPI app
│     │  │  ├─ cli.py                 # bamboo-dashboard entry point
│     │  │  └─ static/index.html      # single-page monitoring dashboard
│     │  ├─ dummy_agent/
│     │  ├─ dast_agent/              # planned
│     │  ├─ index_builder_agent/     # planned
│     │  ├─ feedback_agent/          # planned
│     │  └─ metrics_agent/           # planned
│     ├─ plugin/                     # Bamboo MCP plugin adapter
│     └─ resources/
│        └─ config/
│           ├─ ingestion-agent.yaml
│           ├─ cric-agent.yaml
│           ├─ github-doc-sync-agent.yaml
│           └─ supervisor-agent.yaml
├─ tests/
│  └─ agents/
│     ├─ ingestion_agent/
│     ├─ cric_agent/
│     ├─ github_doc_sync_agent/
│     ├─ supervisor_agent/
│     │  └─ test_supervisor_agent.py
│     ├─ dummy_agent/
│     └─ test_base_agent.py
└─ .github/
   └─ workflows/
      └─ ci.yml
```

---

## Shared tooling

Scripts draw on shared components in `common/`:

- **CLI utilities** — `common/cli.py` provides `log_startup_banner()`, called by every script on startup to emit a consistent `prog  version=X.Y.Z  python=A.B.C` log line
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

**Script logs wrong version after `bump_version.py`** — `importlib.metadata` reads
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

**`document-monitor` — ChromaDB collection appears empty after upgrade** — the script now stores vectors in slotted collection names (`atlas_docs__a` / `atlas_docs__b`) rather than the bare logical name (`atlas_docs`).  If you are upgrading from an older version, the script will not find or use any data in the old unslotted collection.  Wipe and re-ingest:
```bash
rm -rf .chromadb/ .document_monitor/
bamboo-document-monitor \
  --watch /data/bamboo/rag/panda_docs  panda_docs \
  --watch /data/bamboo/rag/atlas_docs  atlas_docs \
  --chroma-dir /data/bamboo/.chromadb --once
```

**`document-monitor` — ChromaDB dimension mismatch error** — this used to require manually deleting the collection.  The blue/green design now handles it automatically: the idle slot is deleted and recreated from scratch before every update, so a changed embedder model can never corrupt the live collection.  If you see a dimension-mismatch error in the logs it means the current cycle failed to build into the idle slot; the live collection remains readable and the script will retry on the next cycle.

 — the embedding
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
(`pytest`) on every push. All scripts and shared tools must have corresponding
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
