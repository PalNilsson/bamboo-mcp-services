# Developer setup guide

This document covers everything needed to get the repository running locally,
continue existing development, and follow the project's workflow conventions.

---

## Returning developer — quick resume

If you have already set up the environment and just want to continue:

```bash
cd bamboo-mcp-services
conda activate bamboo-mcp-services
pip install -e .          # pick up any dependency changes since last time
pytest                    # confirm everything still passes
```

That is all you need on a normal working day.

---

## First-time setup

### Prerequisites

- [Miniforge](https://github.com/conda-forge/miniforge) or
  [Miniconda](https://docs.conda.io/en/latest/miniconda.html) installed
- Python 3.10 or later (managed by conda)
- Git

### 1 — Clone the repository

```bash
git clone <repo-url>
cd bamboo-mcp-services
```

### 2 — Create the conda environment

```bash
conda create -n bamboo-mcp-services python=3.12
conda activate bamboo-mcp-services
```

The environment is named `bamboo-mcp-services` throughout this document.  If you choose
a different name, substitute it wherever `bamboo-mcp-services` appears.

> **Important:** run these commands one at a time, not pasted as a block.
> If `conda activate` is pasted with a trailing `\n` or as part of a
> multi-line paste, the shell may silently skip the activation and the
> subsequent `pip install` will target the wrong Python.  Always verify
> with `which python` — the path should contain `bamboo-mcp-services`
> before continuing.

### 3 — Install Python dependencies

```bash
# Runtime + dev dependencies (pytest, flake8, pytest-cov):
pip install -r requirements.txt
pip install -e ".[dev]"
```

The `-e` flag installs the package in editable mode.  This is required —
the project uses a `src/` layout and the package will not be importable
without it.

### 4 — Install the DuckDB CLI (optional but recommended)

`pip install duckdb` installs the Python library only.  The standalone
`duckdb` command-line binary is a separate install:

```bash
# macOS (recommended):
brew install duckdb

# Or download directly into the conda env (no Homebrew needed):
curl -L https://github.com/duckdb/duckdb/releases/download/v1.2.2/duckdb_cli-osx-universal.zip \
  -o /tmp/duckdb_cli.zip
unzip /tmp/duckdb_cli.zip -d "$CONDA_PREFIX/bin/"
chmod +x "$CONDA_PREFIX/bin/duckdb"
```

> **Note:** `conda install -c conda-forge duckdb` does **not** install the CLI
> binary on macOS — it only installs the Python extension.

### 5 — Install pre-commit hooks

```bash
pip install pre-commit
pre-commit install
```

The hooks run automatically on `git commit`.  They check for trailing
whitespace, large files, flake8 errors, and circular imports.

### 6 — Verify the setup

```bash
# Package is importable:
python -c "from bamboo_mcp_services.agents.cric_agent.agent import CricAgent; print('OK')"

# All tests pass:
pytest

# CLI entry points are registered:
bamboo-cric --help
bamboo-ingestion --help
bamboo-document-monitor --help

# Linting is clean:
flake8 src tests
```

---

## Running the agents locally

### CRIC agent

Reads ATLAS queue metadata from CVMFS and loads it into a local DuckDB file.

```bash
# One-shot — load once and exit (good for testing):
bamboo-cric --data /tmp/cric.db --once --log-level DEBUG

# Daemon mode — re-reads CVMFS every 10 minutes:
bamboo-cric \
  --data /tmp/cric.db \
  --config src/bamboo_mcp_services/resources/config/cric-agent.yaml

# Inspect the result:
duckdb /tmp/cric.db "SELECT queue, status, cloud, tier FROM queuedata LIMIT 10"
```

Full documentation: [README-cric_agent.md](./README-cric_agent.md)

### Ingestion agent

Downloads BigPanda job metadata for configured queues.

```bash
# One-shot (inter-queue delay suppressed):
bamboo-ingestion \
  --config src/bamboo_mcp_services/resources/config/ingestion-agent.yaml \
  --once --log-level DEBUG

# Daemon mode:
bamboo-ingestion \
  --config src/bamboo_mcp_services/resources/config/ingestion-agent.yaml

# Inspect the result:
python scripts/dump_ingestion_db.py --count
python scripts/dump_ingestion_db.py --table jobs --queue BNL --limit 5
```

Full documentation: [README-ingestion_agent.md](./README-ingestion_agent.md)

---

## Development workflow

### Branching

Work on feature branches.  Branch names should be descriptive:
`feature/cric-mcp-tool`, `fix/hash-skip-bug`, `docs/update-handover`.

### Before committing

```bash
flake8 src tests          # must be clean
pytest                    # all tests must pass
```

Pre-commit hooks enforce flake8 and a few other checks automatically on
`git commit`, but running the above manually first avoids surprises.

### Linting rules (`.flake8`)

| Rule | Value |
|---|---|
| `max-line-length` | 160 |
| `ignored` | E262, E265, E266, N804, W504, B902, N818 |
| **E241 is NOT ignored** | Do not align dict values with extra spaces |

```python
# Wrong — triggers E241:
{"key":        "value",
 "longer_key": "value"}

# Correct:
{"key": "value",
 "longer_key": "value"}
```

### Running the full pre-commit suite manually

```bash
pre-commit run --all-files
```

### Adding a new agent

1. Create `src/bamboo_mcp_services/agents/<name>_agent/` with
   `__init__.py`, `agent.py`, `cli.py`, and any fetcher modules.
2. Subclass `Agent` from `agents/base.py` and implement
   `_start_impl`, `_tick_impl`, `_stop_impl`.
3. Register the CLI entry point in `pyproject.toml` under
   `[project.scripts]`.
4. Add a config file under `src/bamboo_mcp_services/resources/config/`.
5. Add tests in `tests/agents/<name>_agent/`.
6. Add a `README-<name>_agent.md` and update `README.md` and `CLAUDE.md`.

The `cric_agent` is the simplest complete example to use as a template
(simpler than `ingestion_agent`: no threads, no HTTP, no background fetcher).

---

## Project layout (key files)

```
bamboo-mcp-services/
├─ README.md                          ← project overview and quick-start
├─ README-cric_agent.md               ← CRIC agent full docs
├─ README-ingestion_agent.md          ← ingestion agent full docs
├─ README-document_monitor_agent.md   ← document monitor full docs
├─ CLAUDE.md                          ← AI assistant context for this repo
├─ CONTRIBUTING.md                    ← you are here
├─ HANDOVER-bamboo-sql-tool.md        ← handover notes for the jobs MCP tool
├─ HANDOVER-cric-mcp-tool.md          ← handover notes for the CRIC MCP tool
├─ pyproject.toml                     ← dependencies, entry points, build config
├─ requirements.txt                   ← flat dep list (mirrors pyproject.toml)
├─ .flake8                            ← linting config
├─ .pre-commit-config.yaml            ← pre-commit hooks
├─ scripts/
│  └─ dump_ingestion_db.py            ← CLI tool to inspect jobs.duckdb
├─ src/bamboo_mcp_services/
│  ├─ agents/
│  │  ├─ base.py                      ← Agent ABC and lifecycle state machine
│  │  ├─ cric_agent/                  ← CRIC queuedata ingestion
│  │  ├─ ingestion_agent/             ← BigPanda jobs ingestion
│  │  ├─ document_monitor_agent/      ← ChromaDB-backed document watcher
│  │  └─ dummy_agent/                 ← minimal no-op agent (template + tests)
│  └─ common/
│     ├─ panda/source.py              ← file/URL fetch with content hashing
│     └─ storage/
│        ├─ duckdb_store.py           ← low-level DuckDB helpers
│        ├─ schema.py                 ← DDL for jobs tables
│        └─ schema_annotations.py    ← field descriptions for LLM prompts
├─ tests/
│  └─ agents/
│     ├─ cric_agent/test_cric_agent.py        ← 43 tests
│     ├─ ingestion_agent/                     ← 19+ tests
│     ├─ dummy_agent/
│     └─ test_base_agent.py
└─ src/bamboo_mcp_services/resources/config/
   ├─ cric-agent.yaml
   └─ ingestion-agent.yaml
```

---

## Common pitfalls

**`ModuleNotFoundError: bamboo_mcp_services` in pytest despite successful `python -c` import**
The conda environment is not active for pytest.  This happens when `conda activate`
was pasted with a trailing `\n` or as part of a multi-line block — the shell
silently skips the activation and `pip install -e .` runs against the system
Python instead.  Fix: run `conda activate bamboo-mcp-services` alone on its own
line, verify with `which python` (should contain `bamboo-mcp-services` in the
path), then re-run `pip install -e ".[dev]"` and `pytest`.

**`ModuleNotFoundError: bamboo_mcp_services`**
Run `pip install -e .` from the repository root.  The `src/` layout means the
package is not importable unless installed.

**Stale `src/askpanda_atlas_agents/` directory after extracting a zip**
If you extracted a `bamboo-mcp-services.zip` over a directory that previously
held the old `askpanda-atlas-agents` project, the old source tree
`src/askpanda_atlas_agents/` may still be present alongside the new
`src/bamboo_mcp_services/`.  Remove it explicitly:
`rm -rf src/askpanda_atlas_agents`
Then re-run `pip install -e .` and `pytest` to confirm everything is clean.

**`duckdb: command not found`**
The `duckdb` CLI binary is separate from the Python package.  Install it with
`brew install duckdb` (macOS) or use the direct download shown in step 4 above.
`conda install -c conda-forge duckdb` does not install the CLI on macOS.

**`flake8` E241 errors**
Do not align dict values with extra spaces after `:`.

**Tests fail after schema changes**
Delete any local `.duckdb` files and re-run the agent to recreate them.
In-memory databases used by tests are always recreated from scratch so tests
should not be affected.

**Pre-commit `circular-import-detector` fails**
Run `pre-commit run --all-files` to see which files are involved, then
restructure the imports to remove the cycle (typically by moving shared code
into `common/`).
