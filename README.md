# AskPanDA-ATLAS Agents

**AskPanDA-ATLAS Agents** is a collection of cooperative, Python-based agents that power the *AskPanDA-ATLAS* plugin for the **Bamboo Toolkit**, supporting the ATLAS Experiment.

> ⚠️ **Early development**
> This repository is under active development. The `document-monitor-agent` and `ingestion-agent` are ready for use. Other agents are planned.

---

## Current status

| Agent | Status |
|---|---|
| `document-monitor-agent` | ✅ Ready |
| `ingestion-agent` | ✅ Ready |
| `dast-agent` | 📋 Planned |
| `supervisor-agent` | 📋 Planned |
| `index-builder-agent` | 📋 Planned |
| `feedback-agent` | 📋 Planned |
| `metrics-agent` | 📋 Planned |

---

## Getting started

### Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

For development (includes pytest and flake8):

```bash
pip install -e ".[dev]"
```

> The project uses a `src/` layout, so the package must be installed before running tests or tools.

### Run the document monitor agent

```bash
askpanda-document-monitor-agent --dir ./documents --poll-interval 10 --chroma-dir .chromadb
```

Full documentation: [README-document_monitor_agent.md](./README-document_monitor_agent.md)

### Run the ingestion agent

```bash
# Download all queues once and exit:
askpanda-ingestion-agent --config src/askpanda_atlas_agents/resources/config/ingestion-agent.yaml --once

# Run as a long-lived daemon (polls every 30 minutes):
askpanda-ingestion-agent --config src/askpanda_atlas_agents/resources/config/ingestion-agent.yaml

# Inspect what was collected:
python scripts/dump_ingestion_db.py --count
python scripts/dump_ingestion_db.py --table jobs --queue SWT2_CPB --limit 5
```

Full documentation: [README-ingestion_agent.md](./README-ingestion_agent.md)

---

## Agents

### `document-monitor-agent` ✅ Ready

Watches a directory for new or changed documents and ingests them into ChromaDB for use in RAG pipelines. Extracts and chunks text from `.pdf`, `.docx`, `.txt`, and `.md` files, computes deterministic chunk IDs, and stores vectors and metadata locally.

→ [Full documentation](./README-document_monitor_agent.md)

### `ingestion-agent` ✅ Ready

Periodically downloads job metadata from [BigPanda](https://bigpanda.cern.ch) for a configured list of ATLAS computing queues and persists the data in a local [DuckDB](https://duckdb.org) database for downstream use by Bamboo / AskPanDA. Stores per-job records, facet summaries, and error frequency tables. Supports one-shot and long-running daemon modes.

Key features:
- Configurable queue list, poll cycle (default: 30 min), and inter-queue delay
- Bulk DataFrame inserts — handles 10k+ jobs per queue in under 2 seconds
- Rotating log file, `--log-level DEBUG` support, clean Ctrl-C / SIGTERM shutdown
- `scripts/dump_ingestion_db.py` for inspecting the database from the command line

→ [Full documentation](./README-ingestion_agent.md)

### `dast-agent` 📋 Planned

Will extract DAST help-list email threads (e.g. via Outlook), convert them into structured JSON, and run a daily digest pass producing cleaned Q/A pairs, thread summaries, tags, and resolution status. Output feeds RAG corpora and optional fine-tuning datasets.

### `supervisor-agent` 📋 Planned

Will act as a control plane — ensuring required agents and services are running, restarting agents on failure, enforcing schedules, and providing a single entry point to bring up the full system.

### `index-builder-agent` 📋 Planned

Will build embedding indices for plugin corpora from sources including DAST digests, documentation, and curated knowledge. May be superseded by the `document-monitor-agent`.

### `feedback-agent` 📋 Planned

Will capture user feedback from AskPanDA (e.g. *helpful / not helpful*) and store it in structured form for later analysis.

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
askpanda-dummy-agent --tick-interval 1.0
```

Stop with Ctrl+C or SIGTERM. When adding a new agent, register its entry point in `pyproject.toml` under `[project.scripts]`.

---

## Repository layout

```
askpanda-atlas-agents/
├─ README.md
├─ README-document_monitor_agent.md
├─ README-ingestion_agent.md
├─ pyproject.toml
├─ requirements.txt
├─ scripts/
│  └─ dump_ingestion_db.py       # inspect the ingestion database from the CLI
├─ src/
│  └─ askpanda_atlas_agents/
│     ├─ common/
│     │  └─ storage/
│     │     ├─ duckdb_store.py       # low-level DuckDB helpers
│     │     ├─ schema.py             # DDL — single source of truth for all tables
│     │     └─ schema_annotations.py # field descriptions for LLM context
│     ├─ agents/
│     │  ├─ base.py                  # Agent lifecycle interface
│     │  ├─ ingestion_agent/
│     │  │  ├─ agent.py
│     │  │  ├─ bigpanda_jobs_fetcher.py
│     │  │  └─ cli.py
│     │  ├─ document_monitor_agent/
│     │  ├─ dummy_agent/
│     │  ├─ dast_agent/              # planned
│     │  ├─ supervisor_agent/        # planned
│     │  ├─ index_builder_agent/     # planned
│     │  ├─ feedback_agent/          # planned
│     │  └─ metrics_agent/           # planned
│     ├─ plugin/                     # Bamboo / AskPanDA plugin adapter
│     └─ resources/
│        └─ config/
│           └─ ingestion-agent.yaml
├─ tests/
│  └─ agents/
│     ├─ ingestion_agent/
│     ├─ dummy_agent/
│     └─ test_base_agent.py
└─ .github/
   └─ workflows/
      └─ ci.yml
```

---

## Shared tooling

Agents draw on shared components in `common/`:

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
pytest --cov=askpanda_atlas_agents --cov-report=term-missing
```

### Linting

```bash
flake8 src tests
pylint src/askpanda_atlas_agents
```

### Common pitfalls

**`ModuleNotFoundError: askpanda_atlas_agents`** — run `pip install -e .` from the repository root (where `pyproject.toml` lives).

**Editable install fails** — confirm that `src/askpanda_atlas_agents/` exists and contains an `__init__.py`.

---

## Continuous integration

GitHub Actions runs linting (`pylint`, `flake8`) and the full unit test suite (`pytest`) on every push. All agents and shared tools must have corresponding unit tests.

---

## Relationship to Bamboo

The `plugin/` package provides the integration layer between AskPanDA-ATLAS Agents and the Bamboo Toolkit, keeping agent logic independent of the UI and orchestration layer.

---

## Contributing

Design feedback and contributions are welcome. This repository currently represents an architectural blueprint guiding development — interfaces are intended to be stable, but implementations will evolve.
