# CLAUDE.md

This file gives Claude (and any other AI assistant) the context needed to work
effectively in this repository.

---

## What this repository is

**Bamboo MCP Services** is a collection of Python services that feed data into
the Bamboo Toolkit, supporting ATLAS Experiment computing operations at CERN.

| Agent | Status | Entry point |
|---|---|---|
| `ingestion-agent` | ✅ Ready | `bamboo-ingestion` |
| `document-monitor-agent` | ✅ Ready | `bamboo-document-monitor` |
| `cric-agent` | ✅ Ready | `bamboo-cric` |
| `github-doc-sync-agent` | ✅ Ready | `bamboo-github-sync` |
| `supervisor-agent` | ✅ Ready | `bamboo-supervisor` |
| `dast-agent` | 📋 Planned | — |

---

## Install and setup

The project uses a **conda environment**, not venv.  The environment must be
Python **3.12** — `torch==2.2.2` (required for the embedding stack) has no
Python 3.13 x86_64 wheel on PyPI.

```bash
# First-time setup:
conda create -n bamboo-mcp-services python=3.12
conda activate bamboo-mcp-services
pip install -r requirements.txt
pip install -e ".[dev]"   # runtime deps + flake8, pytest, pytest-cov

# Returning developer:
conda activate bamboo-mcp-services
pip install -e .          # pick up any dependency changes
pytest                    # should show 201 passed
```

The project uses a `src/` layout — `pip install -e .` must be run before
importing the package or running tests.

> **Pitfall**: if `pip install .` (without `-e`) was ever run, the installed
> copy in `site-packages` will shadow the source tree silently.  Fix with:
> `pip uninstall bamboo-mcp-services -y && pip install -e .`

---

## Running the agents

### Run everything with the supervisor (recommended)

```bash
cp src/bamboo_mcp_services/resources/config/supervisor-agent.yaml ./supervisor-agent.yaml
$EDITOR supervisor-agent.yaml   # adjust paths as needed
bamboo-supervisor --config supervisor-agent.yaml

# Verify config without starting anything:
bamboo-supervisor --config supervisor-agent.yaml --status
```

Stop with **Ctrl-C** or `kill -TERM <pid>`.  All child agents receive SIGTERM
and are given 30 s to exit before SIGKILL.

### Run individual agents

```bash
# GitHub documentation sync — download docs from GitHub repos:
bamboo-github-sync \
  --config src/bamboo_mcp_services/resources/config/github-doc-sync-agent.yaml \
  --once
# Set GITHUB_TOKEN env var for private repos or to raise rate limit to 5000/hour.
# Force re-download by deleting: raw/owner/repo/.sync_state.json

# Document monitor — ingest normalised docs into ChromaDB:
bamboo-document-monitor \
  --dir /abs/path/to/RAG \
  --chroma-dir /abs/path/to/.chromadb \
  --once
# Always use absolute paths for --chroma-dir.
# --dir is created automatically if it does not exist (mkdir -p semantics).
# First run on a new machine needs HF_HUB_OFFLINE=0 to download the model.

# Ingestion agent — download BigPanda job metadata:
bamboo-ingestion \
  --config src/bamboo_mcp_services/resources/config/ingestion-agent.yaml \
  --once

# CRIC agent — load ATLAS queue metadata from CVMFS:
bamboo-cric \
  --data cric.db \
  --once

# All agents support --once (single tick then exit) and daemon mode (loop forever).
# All agents support --log-level DEBUG and --log-file PATH.
```

Inspect databases:

```bash
# BigPanda jobs:
python scripts/dump_ingestion_db.py --count
python scripts/dump_ingestion_db.py --table jobs --queue BNL --limit 5

# CRIC queuedata (requires duckdb CLI — brew install duckdb):
duckdb cric.db "SELECT queue, status, cloud, tier FROM queuedata LIMIT 10"
# Without CLI:
python -c "import duckdb; print(duckdb.connect('cric.db', read_only=True).execute('SELECT COUNT(*) FROM queuedata').fetchone())"
```

---

## Tests and linting

```bash
pytest                                                   # run all 201 tests
pytest --cov=bamboo_mcp_services --cov-report=term-missing
pytest tests/agents/supervisor_agent/ -v                 # supervisor tests
pytest tests/agents/github_doc_sync_agent/ -v            # github sync tests
pytest tests/agents/cric_agent/ -v                       # CRIC agent tests
flake8 src tests                                         # must be clean before commit
```

Linting rules (`.flake8`):
- Max line length: **160**
- Ignored: E262, E265, E266, N804, W503, W504, B902, N818
- Max complexity: **15** (McCabe; enforced via C901)
- Excluded: `.venv`, `__pycache__`, `*.egg-info`, `build`, `dist`
- **E241 (multiple spaces after `:`) is NOT ignored** — do not align dict values
- **E704 (single-line `def`) is NOT ignored** — always expand `def` bodies to
  multiple lines, including in test helper classes

---

## Docstring style

All docstrings must use **Google style**.

```python
def my_function(x: int, y: str) -> bool:
    """One-line summary.

    Args:
        x: Description of x.
        y: Description of y.

    Returns:
        Description of the return value.

    Raises:
        ValueError: If x is negative.
    """
```

---

## Repository layout

```
bamboo-mcp-services/
├─ CLAUDE.md
├─ CHANGELOG.md                        ← release notes (Keep a Changelog format)
├─ README.md
├─ README-supervisor-agent.md
├─ README-ingestion_agent.md
├─ README-document_monitor_agent.md
├─ README-cric_agent.md
├─ README-github_doc_sync_agent.md
├─ HANDOVER-bamboo-sql-tool.md
├─ HANDOVER-cric-mcp-tool.md
├─ HANDOVER-github-doc-sync-agent.md
├─ pyproject.toml
├─ .flake8
├─ scripts/
│  ├─ dump_ingestion_db.py
│  └─ bump_version.py                  ← version bump script (see Versioning below)
├─ src/bamboo_mcp_services/
│  ├─ agents/
│  │  ├─ base.py                       ← Agent ABC and lifecycle state machine
│  │  ├─ supervisor_agent/
│  │  │  ├─ __init__.py
│  │  │  ├─ agent.py                   ← SupervisorAgent; _sigterm_scheduled/daemons helpers
│  │  │  ├─ scheduler.py               ← DaemonState, ScheduledState, AgentConfig (no subprocess)
│  │  │  └─ cli.py                     ← bamboo-supervisor; --status, --once flags
│  │  ├─ github_doc_sync_agent/
│  │  │  ├─ agent.py                   ← GithubDocSyncAgent, GithubDocSyncConfig
│  │  │  ├─ github_doc_syncer.py       ← interval gate, multi-repo loop
│  │  │  ├─ github_markdown_sync.py    ← vendored GitHub API + sync library
│  │  │  └─ cli.py                     ← bamboo-github-sync
│  │  ├─ document_monitor_agent/       ← ChromaDB-backed document watcher
│  │  ├─ ingestion_agent/              ← BigPanda jobs ingestion
│  │  ├─ cric_agent/                   ← CRIC queuedata ingestion
│  │  └─ dummy_agent/                  ← minimal no-op agent (template)
│  └─ common/
│     ├─ cli.py                        ← shared log_startup_banner() helper
│     ├─ panda/source.py               ← file/URL fetch with content hashing
│     └─ storage/
│        ├─ duckdb_store.py
│        ├─ schema.py
│        └─ schema_annotations.py      ← field descriptions for LLM prompts
├─ tests/
│  ├─ test_duckdb_store.py
│  └─ agents/
│     ├─ supervisor_agent/             ← 22 tests
│     │  └─ test_supervisor_agent.py
│     ├─ github_doc_sync_agent/        ← 72 tests
│     ├─ cric_agent/                   ← 46 tests
│     ├─ ingestion_agent/              ← 21 tests
│     ├─ dummy_agent/                  ← 2 tests
│     └─ test_base_agent.py            ← 8 tests
└─ src/bamboo_mcp_services/resources/config/
   ├─ supervisor-agent.yaml
   ├─ github-doc-sync-agent.yaml
   ├─ ingestion-agent.yaml
   └─ cric-agent.yaml
```

---

## supervisor_agent — key design decisions

**Subprocess model, not in-process orchestration**: every managed agent is
launched via its CLI entry point using `subprocess.Popen`.  This avoids
embedding-model memory conflicts (the document-monitor loads a
sentence-transformer) and keeps the supervisor language-agnostic.

**Two modes per agent** (set in `supervisor-agent.yaml`):
- `daemon` — long-running process; supervisor restarts on exit with exponential
  back-off (5 s base, doubles on rapid restarts, capped at 300 s).
- `scheduled` — short-lived `--once` process launched every `interval_s`.
  Supervisor waits for completion; kills if `run_timeout_s` exceeded.

**`scheduler.py` is subprocess-free**: `DaemonState` and `ScheduledState`
contain all scheduling logic and back-off arithmetic without touching
`subprocess`.  This makes unit testing possible without spawning real processes.

**`depends_on_file`**: an agent can declare a file it needs before starting
(e.g. `ingestion` waits for `cric.duckdb`).  The supervisor polls up to
`depends_timeout_s` seconds, then starts the agent anyway with a warning.

**`--status` flag**: prints a JSON config summary and exits without starting
anything.  Useful for verifying configuration on a new machine.

**HTTP health endpoint** (planned, not yet implemented): `SupervisorAgent.health()`
already returns the full per-agent JSON structure the endpoint would serve.
When implemented it will be a thin wrapper; see `TODO: HTTP health endpoint`
comments in `agent.py`.

**C901 / complexity**: `_stop_impl` was refactored into three helpers
(`_sigterm_scheduled`, `_sigterm_daemons`, `_wait_for_daemons`) to stay within
the `max-complexity = 15` flake8 limit.

---

## github_doc_sync_agent — key design decisions

**Vendored library**: `github_markdown_sync.py` is copied verbatim from the
standalone `github-documentation-sync` project.  Do not modify it lightly —
any changes should also be considered for upstreaming.

**Output structure**: Files are written into `destination/owner/repo_name/`
subdirectories (not flat into `destination`).  This avoids name collisions when
multiple repos share a destination root and makes file provenance obvious.

**State file per repo**: `.sync_state.json` lives inside
`destination/owner/repo_name/`, not at the top level.  One state file per repo
entry — fully independent.

**`within_hours` skipped on first run**: The recency gate only fires when a
prior `.sync_state.json` exists.  First run always downloads regardless of
commit age.

**Per-repo failure isolation**: Exceptions in one repo are caught and recorded
but never abort the remaining repos.

**Force re-download**: Delete `destination/owner/repo_name/.sync_state.json`.

---

## document_monitor_agent — key design decisions

**Recursive file discovery**: Uses `rglob("*")` (not `iterdir()`), so it
traverses subdirectories.  This is necessary to pick up files written by the
github sync agent into `owner/repo_name/` subdirectories.

**`--dir` is created automatically**: `_start_impl` calls
`self.directory.mkdir(parents=True, exist_ok=True)`.  A missing `--dir` path
is never an error — the agent creates it and starts with an empty directory.

**`--once` flag**: Runs a single poll cycle and exits.  Use this in cron
pipelines after `bamboo-github-sync --once`.

**Always use absolute paths** for `--chroma-dir` to avoid the database being
written to different locations depending on the working directory.

**First run on a new machine** requires `HF_HUB_OFFLINE=0` to download the
embedding model.  Subsequent runs use the cached model automatically.

---

## CRIC agent — key design decisions

See `HANDOVER-cric-mcp-tool.md` for full detail.  Summary:

- Source: CVMFS file `cric_pandaqueues.json` (~700 queues, ~90 fields each)
- Database: DuckDB `cric.db`, single `queuedata` table, full replace on change
- Hash-based skip: file is SHA-256 hashed; DB write skipped when unchanged
- Dynamic type inference: column types inferred from data, not fixed DDL
- `--data PATH` is a required CLI flag, not in the YAML config

---

## Ingestion agent — key design decisions

See `HANDOVER-bamboo-sql-tool.md` for full detail.  Summary:

- Source: BigPanda HTTP API per queue
- Database: DuckDB `jobs.duckdb`, three tables: `jobs`, `selectionsummary`, `errors_by_count`
- Bulk inserts via pandas DataFrame (not `executemany`)
- 60s inter-queue delay in daemon mode; skipped in `--once` mode
- **CRIC-driven queue discovery**: `bigpanda_jobs.cric_path` in the YAML points
  to `cric_pandaqueues.json`; top-level JSON keys are the PanDA queue names.
  Falls back to the `queues` list if the file is absent.
- **`max_queues`**: caps the number of queues processed per cycle.  Override at
  runtime with `--max-queues N`.

---

## Annotated schema for LLM context

```python
from bamboo_mcp_services.common.storage.schema_annotations import (
    get_schema_context,             # jobs.duckdb — all three tables
    get_queuedata_schema_context,   # cric.db — queuedata table
)
```

---

## Agent lifecycle

All agents implement the `Agent` ABC from `agents/base.py`:

```python
agent.start()   # → RUNNING; initialises resources
agent.tick()    # → one unit of work; raises if not RUNNING
agent.health()  # → HealthReport (state, timestamps, agent-specific details)
agent.stop()    # → STOPPED; releases resources
```

When adding a new agent:
1. Subclass `Agent`, implement `_start_impl`, `_tick_impl`, `_stop_impl`
2. Add a `cli.py` with `--once`, `--log-level`, `--log-file`, SIGTERM handler
3. Register in `pyproject.toml` under `[project.scripts]`
4. Add config YAML under `resources/config/`
5. Add an entry to `supervisor-agent.yaml` (daemon or scheduled mode)
6. Add tests in `tests/agents/<n>_agent/`
7. Add `README-<n>_agent.md`, update `README.md` and this file

Use `cric_agent` as the simplest template (no threads, no HTTP, no background
fetcher).  Use `github_doc_sync_agent` as the template for agents that call
external HTTP APIs.

---

## Versioning

The package version lives in one place: the `version` field in `pyproject.toml`.

To cut a new release, use the bump script:

```bash
python scripts/bump_version.py <old_version> <new_version>
# Example:
python scripts/bump_version.py 1.0.0 1.1.0
```

The script validates both strings against PEP 440, updates `pyproject.toml`,
reports the change, and exits non-zero on any failure.  After bumping, follow
the printed reminder and reinstall so agents pick up the new version at runtime:

```bash
pip install -e .
```

`importlib.metadata` reads the version from the installed package metadata, not
directly from `pyproject.toml`.  If you skip the reinstall, agents will still
log the old version.

---

## Startup banner

Every agent CLI logs a startup banner immediately after logging is configured:

```
bamboo-cric  version=1.0.0  python=3.12.3
```

This is implemented in `bamboo_mcp_services.common.cli.log_startup_banner()`.
All CLI entry points call it — do the same when adding a new agent:

```python
from bamboo_mcp_services.common.cli import log_startup_banner

def main(argv=None):
    args = build_parser().parse_args(argv)
    _configure_logging(args.log_file, args.log_level)
    log_startup_banner(logger, "bamboo-<agent-name>")
    ...
```

---

## Concurrency safety — DuckDB

**Writers**: Each agent holds the single writable DuckDB connection for its
database file.  All multi-statement write operations (table replacement, queue
cycle updates) are wrapped in explicit `BEGIN` / `COMMIT` / `ROLLBACK`
transactions so concurrent readers never observe a torn state.

**Readers** (AskPanDA / Bamboo MCP): Always open DuckDB files with
`read_only=True`.  DuckDB enforces a single-writer policy — a second
`read_only=False` connection while the agent is running will either block or
raise `IOException: Database is already open`.  `read_only=True` connections
are explicitly allowed to coexist with one writer, and DuckDB's MVCC ensures
they see a consistent committed snapshot:

```python
conn = duckdb.connect(database="cric.db", read_only=True)
conn = duckdb.connect(database="jobs.duckdb", read_only=True)
```

---

## Test writing conventions

**Mock subprocess correctly**: `MagicMock(spec=subprocess.Popen)` raises
`InvalidSpecError` in Python 3.12+ when called inside a `@patch` decorator
that has already replaced `subprocess.Popen` with a Mock.  Always capture the
real class before any patch runs:

```python
import subprocess
_REAL_POPEN = subprocess.Popen   # module-level, before any @patch

def _make_mock_proc(pid=1234, returncode=None):
    proc = MagicMock(spec=_REAL_POPEN)
    proc.pid = pid
    proc.returncode = returncode
    proc.poll.return_value = returncode
    proc.wait.return_value = returncode
    return proc
```

**Patch at the consumer, not the source**: patch
`bamboo_mcp_services.agents.supervisor_agent.agent.os.path.exists`, not
`os.path.exists`.  The logging `RotatingFileHandler` also calls `os.path.exists`
internally — patching the global with a finite `side_effect` list will be
exhausted by log emissions before the test code runs.  Use a `side_effect`
function that only intercepts the specific path under test and delegates
everything else to the real `os.path.exists`.

**`time.monotonic` mock call counts**: `_wait_for_dependency` and
`_launch_daemon` both call `time.monotonic()`.  When mocking it, provide enough
values for the full call chain (dependency wait loop + back-off check in
`_launch_daemon`).  Count calls carefully; a list that is one short raises
`StopIteration`.

**YAML boolean traps**: bare `off`, `no`, `false`, `on`, `yes`, `true` are all
parsed as booleans by PyYAML (YAML 1.1).  Quote agent names and command
arguments in test YAML strings when they could be misinterpreted:
`name: "off"`, not `name: off`.

---

## Common pitfalls

**`ModuleNotFoundError: bamboo_mcp_services`** — run `pip install -e .` from
the repo root.

**Agent still logs old version after `bump_version.py`** — `importlib.metadata`
reads the version baked in at install time, not live from `pyproject.toml`.
Run `pip install -e .` after every bump.

**`pip install -e .` fails with "no matching distribution for torch==2.2.2"** —
you are running in the base conda environment (Python 3.13) rather than the
`bamboo-mcp-services` environment (Python 3.12).  Run
`conda activate bamboo-mcp-services` first; verify with `python --version`.

**`duckdb: command not found`** — `pip install duckdb` installs the Python
package only, not the CLI binary.  Use `brew install duckdb` on macOS.

**flake8 E241** — do not align dict values with extra spaces after `:`.

**flake8 C901** — if a method exceeds `max-complexity = 15`, extract helpers
rather than suppressing the warning.  See `_stop_impl` in `supervisor_agent/agent.py`
for the pattern.

**`time.sleep` mock in tests causes infinite loop** — mock
`BigPandaJobsFetcher._interruptible_sleep` directly instead.

**github-doc-sync skipping repos unexpectedly** — check that
`destination/owner/repo_name/.sync_state.json` does not already exist from a
previous run.  Delete it to force a re-download.

**document-monitor not picking up new files** — confirm `--dir` points at the
normalised output directory, not the raw download directory.

**supervisor agent not restarting a crashed daemon** — check
`supervisor-agent.log` for `Rapid-restart back-off` warnings.  The back-off
resets when the supervisor itself is restarted.
