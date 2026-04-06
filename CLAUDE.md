# CLAUDE.md

This file gives Claude (and any other AI assistant) the context needed to work
effectively in this repository.

---

## What this repository is

**AskPanDA-ATLAS Agents** is a collection of Python agents that feed data into
the *AskPanDA-ATLAS* plugin for the Bamboo Toolkit, which supports ATLAS
Experiment computing operations at CERN.

Two agents are production-ready; others are planned:

| Agent | Status | Entry point |
|---|---|---|
| `ingestion-agent` | ✅ Ready | `askpanda-ingestion-agent` |
| `document-monitor-agent` | ✅ Ready | `askpanda-document-monitor-agent` |
| `cric-agent` | ✅ Ready | `askpanda-cric-agent` |
| `dast-agent` | 📋 Planned | — |
| `supervisor-agent` | 📋 Planned | — |

---

## Install and setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .          # installs runtime deps + registers CLI entry points
pip install -e ".[dev]"   # adds flake8, pytest, pytest-cov
```

Runtime dependencies (declared in `pyproject.toml [project.dependencies]`):
`duckdb>=0.10`, `requests>=2.31`, `pyyaml>=6.0`, `pandas>=2.0`.

The project uses a `src/` layout — `pip install -e .` must be run before
importing the package or running tests.

---

## Running the agents

```bash
# Ingestion agent — download all queues once and exit:
askpanda-ingestion-agent \
  --config src/askpanda_atlas_agents/resources/config/ingestion-agent.yaml \
  --once

# Ingestion agent — daemon mode (polls every 30 minutes):
askpanda-ingestion-agent \
  --config src/askpanda_atlas_agents/resources/config/ingestion-agent.yaml

# Useful debug flags:
#   --log-level DEBUG
#   --inter-queue-delay 0     # skip the 60s wait between queues
#   --log-file ""             # disable file logging

# Inspect the resulting database:
python scripts/dump_ingestion_db.py --count
python scripts/dump_ingestion_db.py --table jobs --queue BNL --limit 5
python scripts/dump_ingestion_db.py --table jobs --queue BNL --format json | jq '.pandaid'

# CRIC agent — load queuedata once and exit:
askpanda-cric-agent \
  --data cric.db \
  --once

# CRIC agent — daemon mode (re-reads CVMFS file every 10 minutes):
askpanda-cric-agent \
  --data cric.db \
  --config src/askpanda_atlas_agents/resources/config/cric-agent.yaml

# Useful debug flags (same as ingestion agent):
#   --log-level DEBUG
#   --log-file ""             # disable file logging

# Inspect the resulting database (requires duckdb CLI — see note below):
duckdb cric.db "SELECT COUNT(*) FROM queuedata"
duckdb cric.db "SELECT queue, status, cloud, tier FROM queuedata LIMIT 10"
# If the duckdb CLI is not installed, use Python instead:
# python -c "import duckdb; print(duckdb.connect('cric.db', read_only=True).execute('SELECT queue, status, cloud, tier FROM queuedata LIMIT 10').df())"
```

---

## Tests and linting

```bash
pytest                                              # run all tests
pytest --cov=askpanda_atlas_agents --cov-report=term-missing
pytest tests/agents/ingestion_agent/ -v            # ingestion agent tests only
pytest tests/agents/cric_agent/ -v                 # CRIC agent tests only
flake8 src tests                                    # must be clean before commit
```

Linting rules (`.flake8`):
- Max line length: **160**
- Ignored: E262, E265, E266, N804, W504, B902, N818
- **E241 (multiple spaces after `:`) is NOT ignored** — do not align dict values

Pre-commit hooks run trailing-whitespace, large-file checks, flake8, and a
circular-import detector.  Run manually with `pre-commit run --all-files`.

---

## Docstring style

All docstrings must use **Google style**.  Every public function, method, and
class requires a docstring.  Scripts (`scripts/`) follow the same convention.

```python
def my_function(x: int, y: str) -> bool:
    """One-line summary.

    Longer description if needed.

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
askpanda-atlas-agents/
├─ CLAUDE.md                          ← you are here
├─ README.md                          ← project overview and quick-start
├─ README-ingestion_agent.md          ← ingestion agent full docs
├─ README-document_monitor_agent.md   ← document monitor full docs
├─ README-cric_agent.md               ← CRIC agent full docs
├─ HANDOVER-bamboo-sql-tool.md        ← handover notes for the Bamboo SQL tool
├─ pyproject.toml                     ← dependencies, entry points, build config
├─ requirements.txt                   ← flat dep list (mirrors pyproject.toml)
├─ .flake8                            ← linting config
├─ .pre-commit-config.yaml            ← pre-commit hooks
├─ scripts/
│  └─ dump_ingestion_db.py            ← CLI tool to inspect jobs.duckdb
├─ src/askpanda_atlas_agents/
│  ├─ agents/
│  │  ├─ base.py                      ← Agent ABC and lifecycle state machine
│  │  ├─ ingestion_agent/
│  │  │  ├─ agent.py                  ← IngestionAgent, config dataclasses
│  │  │  ├─ bigpanda_jobs_fetcher.py  ← BigPanda download loop + DB writes
│  │  │  └─ cli.py                    ← CLI entry point
│  │  ├─ cric_agent/
│  │  │  ├─ agent.py                  ← CricAgent, CricAgentConfig
│  │  │  ├─ cric_fetcher.py           ← file read, hash check, DROP/CREATE/INSERT
│  │  │  └─ cli.py                    ← CLI entry point (askpanda-cric-agent)
│  │  ├─ document_monitor_agent/      ← ChromaDB-backed document watcher
│  │  └─ dummy_agent/                 ← minimal no-op agent (template + tests)
│  └─ common/
│     ├─ panda/
│     │  └─ source.py                 ← file/URL fetch with content hashing
│     └─ storage/
│        ├─ duckdb_store.py           ← low-level DuckDB helpers
│        ├─ schema.py                 ← DDL + apply_schema() + migration (jobs tables)
│        └─ schema_annotations.py    ← field descriptions for jobs + queuedata tables
├─ tests/
│  └─ agents/
│     ├─ ingestion_agent/
│     │  ├─ test_bigpanda_jobs_fetcher.py   ← 18 tests
│     │  └─ test_ingestion_agent.py
│     ├─ cric_agent/
│     │  └─ test_cric_agent.py              ← 43 tests
│     ├─ dummy_agent/test_dummy_agent.py
│     └─ test_base_agent.py                 ← 8 lifecycle tests
└─ src/askpanda_atlas_agents/resources/config/
   ├─ ingestion-agent.yaml            ← default ingestion agent configuration
   └─ cric-agent.yaml                 ← default CRIC agent configuration
```

---

## CRIC agent — key design decisions

**Source**: CVMFS file
`/cvmfs/atlas.cern.ch/repo/sw/local/etc/cric_pandaqueues.json`.
Top-level dict of `{queue_name: {field: value, ...}}` — currently ~700 queues,
~90 fields each.

**Database**: DuckDB file (path set via `--data PATH`, no default).  Single
table `queuedata` — full replace on each changed load, no history.  A
`snapshots` table (from `DuckDBStore`) records one audit row per fetch attempt.

**Hash-based skip**: On every poll the file is read and SHA-256 hashed.  The
DB write is skipped entirely when the hash matches the previous load.  This is
the normal case between CVMFS refresh cycles (~30 min propagation delay).

**Dynamic type inference**: Column types are inferred from the data at load
time (`BIGINT`, `DOUBLE`, `TEXT`) rather than from a fixed DDL.  CRIC adds and
renames fields without notice; dynamic inference avoids breakage.  Booleans
require explicit handling because Python `bool` is a subclass of `int` — the
`_to_cell_value` function checks `isinstance(v, bool)` before `isinstance(v,
int)` to store them as TEXT rather than BIGINT.

**`_data`-suffix fields dropped**: `coreenergy_data`, `corepower_data`, and
`maxdiskio_data` are internal CRIC resolution-chain dicts.  They are stripped
in `_build_rows` via `_SKIP_FIELDS` and never written to the database.

**`--data PATH` required CLI flag**: The DuckDB path is not in the YAML config.
Keeping it as a required flag makes it impossible to run the agent without
explicitly choosing where the database lives, which prevents accidental
overwrites in shared environments.

**DuckDB concurrency**: DuckDB allows multiple readers but only one writer.
The CRIC agent writes to `cric.db`; the ingestion agent writes to `jobs.duckdb`
(or whatever path is configured).  These are separate files — no write
conflicts.  AskPanDA / Bamboo should open both files read-only.

---

## Ingestion agent — key design decisions

**BigPanda source**: `https://bigpanda.cern.ch/jobs/?computingsite=<QUEUE>&json&hours=1`
Returns jobs active in the last hour for one queue.  Hardcoded queues for now:
`SWT2_CPB`, `BNL` (configured in `ingestion-agent.yaml`).

**Database**: DuckDB file (`jobs.duckdb` by default).  Three data tables:
- `jobs` — one row per PanDA job, upserted on `pandaid`; accumulates history
- `selectionsummary` — facet counts per queue, replaced each cycle
- `errors_by_count` — ranked error frequency per queue, replaced each cycle

**Bulk inserts**: All DB writes use `pandas.DataFrame` + `INSERT … SELECT * FROM df`
rather than `executemany`.  This is ~4000× faster for 10k-row payloads (DuckDB
is a columnar engine optimised for bulk operations, not row-by-row inserts).

**Inter-queue delay**: 60 seconds between queue downloads in daemon mode, to
avoid overloading the server.  Skipped automatically in `--once` mode and
overridable with `--inter-queue-delay 0`.

**Ctrl-C handling**: DuckDB converts `KeyboardInterrupt` into
`RuntimeError("Query interrupted")` during query execution.  The fetcher detects
this via `exc.__context__` and re-raises as `KeyboardInterrupt` so the CLI
shutdown path fires correctly.

**Schema migrations**: `apply_schema()` in `schema.py` runs a migration check
before creating tables.  Currently handles one historical migration: the
`selectionsummary` and `errors_by_count` tables had a single-column `id`
primary key that caused constraint violations when two queues were inserted;
this was fixed to a composite `PRIMARY KEY (id, _queue)`.

---

## Annotated schema for LLM context

`schema_annotations.py` provides plain-English descriptions of every database
column, intended for injection into LLM system prompts.  It covers two
databases:

**BigPanda jobs** (`jobs.duckdb`):

```python
from askpanda_atlas_agents.common.storage.schema_annotations import get_schema_context

# Returns a multi-line "Table: … column TYPE description" block
context = get_schema_context()                  # all three tables
context = get_schema_context(["jobs"])          # jobs only
```

**CRIC queuedata** (`cric.db`):

```python
from askpanda_atlas_agents.common.storage.schema_annotations import (
    get_queuedata_schema_context,
    QUEUEDATA_FIELD_DESCRIPTIONS,
)

context = get_queuedata_schema_context()        # queuedata table
```

See `HANDOVER-bamboo-sql-tool.md` for how to use this when building the
Bamboo text-to-SQL tool.

---

## Agent lifecycle

All agents implement the `Agent` ABC from `agents/base.py`:

```python
agent.start()   # → RUNNING; initialises resources
agent.tick()    # → executes one unit of work; raises if not RUNNING
agent.health()  # → HealthReport (state, last tick/success/error timestamps)
agent.stop()    # → STOPPED; releases resources
```

`tick_once()` is an `IngestionAgent`-specific variant that passes `one_shot=True`
to the fetcher, suppressing the inter-queue delay for one-shot CLI invocations.

When adding a new agent:
1. Subclass `Agent` and implement `_start_impl`, `_tick_impl`, `_stop_impl`
2. Add a `cli.py` entry point
3. Register in `pyproject.toml` under `[project.scripts]`
4. Add tests (follow `tests/agents/dummy_agent/` as a template)

The `cric_agent` is a good second reference example: simpler than
`ingestion_agent` (no threads, no BigPanda API), and demonstrates the
hash-based skip pattern for file-backed sources.

---

## Common pitfalls

**`duckdb: command not found`** — `pip install duckdb` (or `pip install -e .`)
installs the Python package only, not the standalone CLI binary.  On macOS the
recommended install is `brew install duckdb`.  Alternatively, download the
binary directly into your conda env:
`curl -L https://github.com/duckdb/duckdb/releases/download/v1.2.2/duckdb_cli-osx-universal.zip -o /tmp/duckdb_cli.zip && unzip /tmp/duckdb_cli.zip -d "$CONDA_PREFIX/bin/" && chmod +x "$CONDA_PREFIX/bin/duckdb"`.
Note: `conda install -c conda-forge duckdb` does **not** install the CLI binary
on macOS.  If you prefer no CLI install, query via Python:
`python -c "import duckdb; print(duckdb.connect('cric.db', read_only=True).execute('SELECT COUNT(*) FROM queuedata').fetchone())"`.

**`ModuleNotFoundError: askpanda_atlas_agents`** — run `pip install -e .` from
the repository root.

**DuckDB constraint errors after schema changes** — delete `jobs.duckdb` and
let the agent recreate it, or rely on `apply_schema()` which runs migrations
automatically.

**`flake8` E241 errors** — do not align dict values with extra spaces; use a
single space after `:` in all dict literals.

**`time.sleep` mock in tests causes infinite loop** — `_interruptible_sleep`
loops on `time.monotonic()`; mocking `time.sleep` without also mocking
`time.monotonic` causes an infinite loop.  Always mock
`BigPandaJobsFetcher._interruptible_sleep` directly instead.

**`json.dumps` emitting `NaN`/`Infinity`** — DuckDB returns Python `float('nan')`
for null-ish float values.  `json.dumps` emits bare `NaN` which is not valid
JSON and breaks `jq`.  Use `_to_json_safe()` from `dump_ingestion_db.py` to
convert non-finite floats to `None` before serialising.