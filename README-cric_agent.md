# cric-agent

A periodic ingestion agent that reads ATLAS queue metadata from the CRIC
(Computing Resource Information Catalogue) and stores the latest snapshot in a
local [DuckDB](https://duckdb.org) database for downstream use by Bamboo /
AskPanDA.

---

## What it does

- Reads `cric_pandaqueues.json` from CVMFS on a configurable interval
  (default: every 10 minutes).
- Compares the SHA-256 hash of the file against the previous load and skips
  the database write when the content is unchanged — avoiding unnecessary churn
  between CVMFS refresh cycles.
- Performs a full **replace** of the `queuedata` table on each changed load:
  `DROP + CREATE + INSERT`. No history is accumulated; the table always
  reflects the latest CRIC snapshot.
- Infers DuckDB column types dynamically from the data (BIGINT, DOUBLE, TEXT)
  so schema changes in CRIC are handled without code changes.
- Drops three internal `_data`-suffix fields
  (`coreenergy_data`, `corepower_data`, `maxdiskio_data`) that carry no
  queryable information.

---

## Database

### File

The DuckDB output file is specified at runtime via `--data PATH`. There is no
default — the path must be supplied explicitly so different deployments can
direct output to different locations without editing the config file.

```bash
askpanda-cric-agent --data /path/to/cric.db
```

### Table: `queuedata`

One row per ATLAS computing queue. The top-level key from
`cric_pandaqueues.json` (the queue name) is stored in the `queue` column.
All other payload fields become columns; structured values (dicts, lists) are
serialised to JSON TEXT.

Selected columns of particular interest:

| Column | Type | Description |
|---|---|---|
| `queue` | `VARCHAR` | PanDA queue identifier (top-level JSON key) |
| `status` | `VARCHAR` | Brokerage status: `online`, `offline`, `test`, `brokeroff` |
| `state` | `VARCHAR` | CRIC record state, e.g. `ACTIVE`, `INACTIVE` |
| `cloud` | `VARCHAR` | Logical PanDA cloud code, e.g. `US`, `DE`, `CERN` |
| `country` | `VARCHAR` | Full country name, e.g. `United States` |
| `tier` | `VARCHAR` | WLCG tier label, e.g. `T1`, `T2`, `T2D` |
| `tier_level` | `BIGINT` | Numeric tier level: 1, 2, or 3 |
| `corecount` | `BIGINT` | CPU cores allocated per job |
| `corepower` | `DOUBLE` | Benchmark power per core in HS06 |
| `maxrss` | `BIGINT` | Maximum RSS memory per job in MB |
| `maxtime` | `BIGINT` | Maximum wall-clock time per job in seconds |
| `nodes` | `BIGINT` | Number of worker nodes at the site |
| `harvester` | `VARCHAR` | Associated Harvester service instance |
| `pilot_manager` | `VARCHAR` | Pilot submission framework |
| `queues` | `TEXT` (JSON) | List of CE endpoint records for this queue |
| `acopytools` | `TEXT` (JSON) | Copy-tool configuration by activity type |
| `astorages` | `TEXT` (JSON) | Storage (RSE) assignments by activity type |
| `params` | `TEXT` (JSON) | Harvester / unified-dispatch configuration overrides |
| `last_modified` | `VARCHAR` | UTC timestamp of last modification in CRIC |

The full data dictionary — descriptions and DuckDB access examples for every
column — is in:

```
src/askpanda_atlas_agents/common/storage/schema_annotations.py
```

See `QUEUEDATA_FIELD_DESCRIPTIONS` and `get_queuedata_schema_context()`.

---

## Installation & setup

### Step 1 — Install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .
```

For development (includes pytest and flake8):

```bash
pip install -e ".[dev]"
```

> The project uses a `src/` layout. The package must be installed
> (`pip install -e .`) before running the CLI or tests.

### Step 2 — Verify

```bash
python -c "from askpanda_atlas_agents.agents.cric_agent.agent import CricAgent; print('OK')"
```

### Step 3 — Confirm CVMFS access

```bash
ls /cvmfs/atlas.cern.ch/repo/sw/local/etc/cric_pandaqueues.json
```

If this path is not available, set `cric_path` in the config file to point to
a local copy of the JSON.

---

## Configuration

The agent is configured via a YAML file. The default path is:

```
src/askpanda_atlas_agents/resources/config/cric-agent.yaml
```

### Full example

```yaml
# Path to cric_pandaqueues.json on CVMFS.
cric_path: /cvmfs/atlas.cern.ch/repo/sw/local/etc/cric_pandaqueues.json

# How often to re-read the file and reload the database, in seconds.
refresh_interval_s: 600   # 10 minutes

# How long to sleep between tick() calls.
tick_interval_s: 60.0
```

### Options

| Key | Default | Description |
|---|---|---|
| `cric_path` | *(required)* | Path to `cric_pandaqueues.json`. No default — must be set explicitly. |
| `refresh_interval_s` | `600` | Minimum seconds between file reads. The file is only re-read when this interval has elapsed. |
| `tick_interval_s` | `60.0` | Seconds between `tick()` calls in the run loop. The tick is a no-op when the interval has not elapsed. |

> **`--data PATH`** is not in the YAML. It is a required CLI flag. This
> separates the deployment-specific database path from the portable config
> file.

---

## Running the agent

### One-shot (recommended for first use and testing)

```bash
askpanda-cric-agent --data /path/to/cric.db --once
```

Reads the file, loads the database, logs a summary, and exits. No daemon
process is left running. Useful for verifying the setup, for cron-based
scheduling, or for scripted data pulls.

### Long-running daemon

```bash
askpanda-cric-agent \
  --data /path/to/cric.db \
  --config src/askpanda_atlas_agents/resources/config/cric-agent.yaml
```

Loops indefinitely, calling `tick()` every `tick_interval_s` seconds.
The file is re-read at most once per `refresh_interval_s`. Stop with
Ctrl-C or SIGTERM — both trigger a clean shutdown and log the final state.

### All command-line options

| Option | Default | Description |
|---|---|---|
| `--data PATH` | *(required)* | Path to the DuckDB output file, e.g. `cric.db`. |
| `--config PATH`, `-c` | `src/.../cric-agent.yaml` | Path to the YAML configuration file. |
| `--once` | off | Run a single tick then exit. |
| `--log-file PATH` | `cric-agent.log` | Rotating log file (10 MB × 5 backups). Pass `""` to disable file logging. |
| `--log-level LEVEL` | `INFO` | Minimum log level for console and file output. One of `DEBUG`, `INFO`, `WARNING`, `ERROR`. |

### First-run walkthrough

```bash
# 1. Read from CVMFS, write to /tmp/cric.db, exit immediately:
askpanda-cric-agent --data /tmp/cric.db --once --log-level DEBUG

# 2. Inspect what was loaded:
duckdb /tmp/cric.db "SELECT COUNT(*) FROM queuedata"
duckdb /tmp/cric.db "SELECT queue, status, cloud, tier FROM queuedata LIMIT 10"

# 3. If everything looks good, run as a daemon:
askpanda-cric-agent --data /data/cric.db --log-file /var/log/cric-agent.log
```

> **Note:** `duckdb` in the commands above is the standalone CLI binary, which
> is separate from the `duckdb` Python package installed by `pip install -e .`.
> See [Querying the database](#querying-the-database) below for how to install
> it, or use the Python alternative if you prefer not to install the CLI.
---

## Querying the database

The DuckDB file can be opened directly by AskPanDA, a Jupyter notebook, or the
`duckdb` CLI. The agent holds the file open in read-write mode while running;
open it read-only from other processes to avoid conflicts.

### Installing the DuckDB CLI

`pip install duckdb` (or `pip install -e .`) installs the Python package only —
it does **not** install the `duckdb` command-line binary. The CLI is a separate
install.

**Recommended — Homebrew (macOS):**

```bash
brew install duckdb
```

**Alternative — direct download into conda env (no Homebrew):**

```bash
curl -L https://github.com/duckdb/duckdb/releases/download/v1.2.2/duckdb_cli-osx-universal.zip \
  -o /tmp/duckdb_cli.zip
unzip /tmp/duckdb_cli.zip -d "$CONDA_PREFIX/bin/"
chmod +x "$CONDA_PREFIX/bin/duckdb"
```

> **Note:** `conda install -c conda-forge duckdb` does **not** install the CLI
> binary on macOS — it only installs the Python extension. Use Homebrew or the
> direct download above.

**Alternative — Python one-liner (no CLI install needed):**

```bash
python -c "
import duckdb
conn = duckdb.connect('/path/to/cric.db', read_only=True)
print(conn.execute('SELECT queue, status, cloud, tier FROM queuedata LIMIT 10').df().to_string())
"
```

### DuckDB CLI

```bash
duckdb /path/to/cric.db
```

Example output:

```
duckdb cric.db "SELECT queue, status, cloud, tier FROM queuedata LIMIT 10"
┌────────────────────┬───────────┬─────────┬─────────┐
│       queue        │  status   │  cloud  │  tier   │
│      varchar       │  varchar  │ varchar │ varchar │
├────────────────────┼───────────┼─────────┼─────────┤
│ AGLT2              │ online    │ US      │ T2D     │
│ AGLT2_MERGE        │ online    │ US      │ T2D     │
│ AGLT2_TEST         │ online    │ US      │ T2D     │
│ ALL                │ offline   │ CERN    │ T0      │
│ AM-01-AANL         │ test      │ DE      │ T3      │
│ ANALY_ARNES_DIRECT │ online    │ ND      │ T3      │
│ ANALY_BNL_VP       │ online    │ US      │ T1      │
│ ANALY_CERN-PTEST   │ brokeroff │ CERN    │ T0      │
│ ANALY_CERN-PTESTM  │ test      │ CERN    │ T0      │
│ ANALY_CERN_T0_ART  │ brokeroff │ CERN    │ T0      │
└────────────────────┴───────────┴─────────┴─────────┘
```

```sql
-- All online queues in the US cloud
SELECT queue, tier, corecount, corepower, nodes
FROM queuedata
WHERE status = 'online' AND cloud = 'US'
ORDER BY tier, queue;

-- Queues managed by Harvester
SELECT queue, harvester, harvester_template
FROM queuedata
WHERE harvester IS NOT NULL
ORDER BY harvester, queue;

-- Queues with CVMFS enabled and direct LAN access
SELECT queue, country, corecount
FROM queuedata
WHERE is_cvmfs = 'True' AND direct_access_lan = 'True'
ORDER BY country, queue;

-- Queues that run HammerCloud AFT tests
SELECT queue, hc_param, hc_suite
FROM queuedata
WHERE json(hc_suite)::STRING LIKE '%"AFT"%';

-- Extract the first CE endpoint from the JSON array
SELECT queue, json_extract(queues, '$[0].ce_endpoint') AS first_ce
FROM queuedata
WHERE queues IS NOT NULL AND queues != '[]'
LIMIT 10;

-- Inspect the full schema
DESCRIBE queuedata;
```

### From Python (AskPanDA / Bamboo)

```python
import duckdb
from askpanda_atlas_agents.common.storage.schema_annotations import (
    get_queuedata_schema_context,
)

# Open read-only while the agent may be running
conn = duckdb.connect("/path/to/cric.db", read_only=True)

# Fetch all online queues as a DataFrame
df = conn.execute(
    "SELECT * FROM queuedata WHERE status = 'online'"
).df()

# Inject the schema summary into an LLM system prompt
schema_context = get_queuedata_schema_context()
system_prompt = f"You have access to a CRIC queuedata database.\n{schema_context}"
```

---

## Refresh behaviour

CVMFS propagates updates from the CERN stratum-0 roughly every 30 minutes,
but the exact timing is not predictable. The agent polls every 10 minutes by
default to keep the local database reasonably fresh without excessive I/O.

On each poll cycle:

1. The file is read and its SHA-256 hash is computed.
2. If the hash matches the previous load, the database write is **skipped** —
   no DROP/CREATE/INSERT occurs.
3. If the hash has changed, the `queuedata` table is replaced in its entirety.

This means the database always holds exactly one snapshot — the most recently
changed version of the file. There is no history table, and the database does
not grow unboundedly over time.

The `snapshots` table (from `DuckDBStore`) records one row per fetch attempt
as a lightweight audit trail, regardless of whether the content changed:

```sql
SELECT * FROM snapshots ORDER BY fetched_utc DESC LIMIT 10;
```

---

## Architecture

```
CricAgent
├── _start_impl()         — opens DuckDBStore, initialises CricQueuedataFetcher
├── _tick_impl()
│   └── CricQueuedataFetcher.run_cycle()
│       ├── interval check (skip if < refresh_interval_s since last attempt)
│       ├── BaseSource.fetch_from_file(cric_path)   — read + SHA-256 hash
│       ├── hash unchanged?  →  skip
│       └── hash changed?
│           ├── _build_rows()     — flatten JSON dict, drop _data fields
│           ├── _infer_schema()   — dynamic type inference across all rows
│           ├── DROP TABLE IF EXISTS queuedata
│           ├── CREATE TABLE queuedata (...)
│           ├── executemany INSERT
│           └── conn.commit()
└── _stop_impl()          — releases fetcher and DuckDB connection
```

Key modules:

| Module | Purpose |
|---|---|
| `agents/cric_agent/agent.py` | Agent lifecycle, `CricAgentConfig` dataclass |
| `agents/cric_agent/cric_fetcher.py` | File reading, hash check, DROP/CREATE/INSERT |
| `agents/cric_agent/cli.py` | CLI entry point (`askpanda-cric-agent`) |
| `common/storage/duckdb_store.py` | Low-level DuckDB helpers (snapshots table) |
| `common/panda/source.py` | File fetching with SHA-256 content hashing |
| `common/storage/schema_annotations.py` | `QUEUEDATA_FIELD_DESCRIPTIONS`, `get_queuedata_schema_context()` |

---

## CI and testing

```bash
pytest tests/agents/cric_agent/ -v
```

The test suite (43 tests) covers:

- **Type inference helpers** — `_to_cell_value` for all Python types including
  the `bool`-before-`int` guard; `_merge_type` widening order; `_infer_schema`
  cross-row widening and in-place mutation.
- **`_build_rows`** — `_data`-suffix field exclusion, `queue` column
  assignment, non-dict payload handling, multi-queue input.
- **`run_cycle`** — first load, hash-based skip, changed-file reload, stale-row
  replacement after reload, interval gate, health attribute updates, file read
  errors, invalid top-level JSON shape.
- **`CricAgent` lifecycle** — `config=None` raises, `start`/`stop`
  idempotency, tick delegation, health report contents before and after first
  load.
- **CLI** — `--data` required, missing config file, missing `cric_path` key,
  end-to-end `--once` run that writes a real DuckDB file and reads it back.

All file I/O is mocked with `unittest.mock.patch`; no CVMFS access or network
is required during tests.

---

## Relationship to AskPanDA / Bamboo

The `cric.db` file is the handoff point between the agent and the Bamboo /
AskPanDA plugin. The plugin opens the file in **read-only** mode and queries
the `queuedata` table directly. The field descriptions in
`schema_annotations.py` are designed to be injected into LLM system prompts
so the model understands what each column means when a user asks a question
that requires a database lookup:

```python
from askpanda_atlas_agents.common.storage.schema_annotations import (
    get_queuedata_schema_context,
)
print(get_queuedata_schema_context())
# Table: queuedata
#   queue                           VARCHAR     PanDA queue identifier ...
#   status                          VARCHAR     Operational status of the queue ...
#   ...
```