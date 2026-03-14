# AskPanDA-ATLAS Agents

**AskPanDA-ATLAS Agents** is a collection of cooperative, Python-based agents that power the *AskPanDA-ATLAS* plugin for the **Bamboo Toolkit**, supporting the ATLAS Experiment.

> ⚠️ **Early development**
> This repository is a preliminary architectural plan with initial scaffolding. Only the `document-monitor-agent` is currently ready for use. Other agents are in active development or planned.

---

## Current status

| Agent | Status |
|---|---|
| `document-monitor-agent` | ✅ Ready |
| `ingestion-agent` | 🔧 In development |
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
pip install -r requirements.txt
pip install -e .
```

For development (includes pytest, flake8, pylint):

```bash
pip install -e ".[dev]"
```

> The project uses a `src/` layout, so the package must be installed before running tests or tools.

### Run the document monitor agent

```bash
askpanda-document-monitor-agent --dir ./documents --poll-interval 10 --chroma-dir .chromadb
```

Full documentation: [README-document_monitor_agent.md](./README-document_monitor_agent.md)

---

## Agents

### `document-monitor-agent` ✅ Ready

Watches a directory for new or changed documents and ingests them into ChromaDB for use in RAG pipelines. Extracts and chunks text from `.pdf`, `.docx`, `.txt`, and `.md` files, computes deterministic chunk IDs, and stores vectors and metadata locally.

→ [Full documentation](./README-document_monitor_agent.md)

### `ingestion-agent` 🔧 In development

Periodically fetches ATLAS queue and site metadata, normalises it, and loads it into DuckDB for fast local queries. Will optionally pull BigPanDA task/job metadata snapshots for debugging and analytics.

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
├─ pyproject.toml
├─ src/
│  └─ askpanda_atlas_agents/
│     ├─ common/                # shared utilities (storage, panda, email, metrics)
│     ├─ agents/
│     │  ├─ ingestion_agent/
│     │  ├─ dast_agent/
│     │  ├─ supervisor_agent/
│     │  ├─ index_builder_agent/
│     │  ├─ feedback_agent/
│     │  └─ metrics_agent/
│     ├─ plugin/                # Bamboo / AskPanDA plugin adapter
│     └─ resources/             # default configs and schemas
├─ tests/
│  ├─ common/
│  ├─ agents/
│  └─ plugin/
├─ deployments/
│  ├─ docker/
│  ├─ systemd/
│  └─ k8s/
└─ .github/
   └─ workflows/
      └─ ci.yml
```

---

## Shared tooling

Agents draw on shared components in `common/`:

- **Storage** — DuckDB, SQLite, filesystem helpers
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
