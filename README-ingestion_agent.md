# ingestion

A periodic data ingestion script that downloads job metadata from the [BigPanda](https://bigpanda.cern.ch) monitoring service for a configured list of ATLAS computing queues and persists the data in a local [DuckDB](https://duckdb.org) database for downstream use by Bamboo / AskPanDA.

---

## What it does

- Polls BigPanda's `/jobs/?computingsite=<QUEUE>&json&hours=1` endpoint for each configured queue on a fixed cycle (default: every 30 minutes).
- Inserts a configurable inter-queue delay (default: 60 seconds) between consecutive queue downloads to avoid overloading the server.
- Persists three categories of data per queue snapshot:
  - **`jobs`** — one row per PanDA job, upserted on `pandaid` so re-runs update existing rows rather than duplicating them.
  - **`selectionsummary`** — per-queue facet statistics (e.g. job status breakdown); replaced on each cycle.
  - **`errors_by_count`** — ranked error-code frequency table with example `pandaid`; replaced on each cycle.
- Continues fetching remaining queues if one queue fails (errors are logged, not fatal).
- Also supports generic file- or URL-based sources (inherited from the base ingestion architecture) independently of the BigPanda jobs cycle.

---

## Database schema

The schema is defined in a dedicated Python module:

```
src/bamboo_mcp_services/common/storage/schema.py
```

This module is the single source of truth for all table DDL. It can be imported by AskPanDA (or any other consumer) to validate, recreate, or introspect the schema without duplicating SQL strings:

```python
import duckdb
from bamboo_mcp_services.common.storage.schema import apply_schema, table_names

conn = duckdb.connect("jobs.duckdb")
apply_schema(conn)          # idempotent — safe to call on an existing database
print(table_names())        # ['jobs', 'selectionsummary', 'errors_by_count']
```

> **Note on DuckDB introspection:** DuckDB exposes full schema discovery via `DESCRIBE <table>` and `information_schema.columns`, so a consuming application can always discover column names and types at runtime without importing this module. The module exists for documentation, reproducibility in tests, and portability — AskPanDA can call `apply_schema()` to guarantee the tables exist before querying.

### `jobs` table

One row per PanDA job. Primary key: `pandaid`.

All columns from the BigPanda `jobs1h` API response are represented. Columns that are always `null` in the current API sample are stored as `VARCHAR` to accommodate future data. Two bookkeeping columns are added by the script:

| Column | Type | Description |
|---|---|---|
| `pandaid` | `BIGINT` | Primary key — unique PanDA job identifier |
| `jobstatus` | `VARCHAR` | e.g. `finished`, `failed`, `running` |
| `computingsite` | `VARCHAR` | Queue / site name |
| `taskid` | `BIGINT` | PanDA task ID |
| `jeditaskid` | `BIGINT` | JEDI task ID |
| `creationtime` | `TIMESTAMP` | Job creation time |
| `modificationtime` | `TIMESTAMP` | Last modification time |
| `cpuefficiency` | `DOUBLE` | CPU efficiency (0–1) |
| `durationsec` | `DOUBLE` | Wall-clock duration in seconds |
| `piloterrorcode` | `INTEGER` | Pilot error code (0 = no error) |
| `piloterrordiag` | `VARCHAR` | Pilot error diagnostic string |
| *(~100 further columns)* | | See `schema.py` for the full list |
| `_queue` | `VARCHAR` | Ingestion script bookkeeping: source queue name |
| `_fetched_utc` | `TIMESTAMP` | Ingestion script bookkeeping: fetch timestamp |

### `selectionsummary` table

One row per facet field per queue, replaced on each cycle. The `list` and `stats` sub-structures vary per field and are stored as JSON.

| Column | Type | Description |
|---|---|---|
| `id` | `INTEGER` | Surrogate key (position in API response array) |
| `field` | `VARCHAR` | Facet field name, e.g. `jobstatus`, `cloud` |
| `list_json` | `JSON` | Array of `{kname, kvalue}` objects |
| `stats_json` | `JSON` | Aggregate stats object (e.g. `{"sum": 42}`) |
| `_queue` | `VARCHAR` | Source queue name |
| `_fetched_utc` | `TIMESTAMP` | Fetch timestamp |

### `errors_by_count` table

One row per error code per queue, replaced on each cycle.

| Column | Type | Description |
|---|---|---|
| `id` | `INTEGER` | Surrogate key (rank in API response) |
| `error` | `VARCHAR` | Error category, e.g. `pilot`, `exe` |
| `codename` | `VARCHAR` | Symbolic error name |
| `codeval` | `INTEGER` | Numeric error code |
| `diag` | `VARCHAR` | Diagnostic string |
| `error_desc_text` | `VARCHAR` | Human-readable description |
| `example_pandaid` | `BIGINT` | A representative job with this error |
| `count` | `INTEGER` | Number of jobs affected in this snapshot |
| `pandalist_json` | `JSON` | Raw list of affected `pandaid` values |
| `_queue` | `VARCHAR` | Source queue name |
| `_fetched_utc` | `TIMESTAMP` | Fetch timestamp |

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

> The project uses a `src/` layout. The package must be installed (`pip install -e .`) before running the CLI or tests.

### Step 2 — Verify

```bash
python -c "from bamboo_mcp_services.agents.ingestion_agent.agent import IngestionAgent; print('OK')"
```

---

## Configuration

The script is configured via a YAML file. The default path is:

```
src/bamboo_mcp_services/resources/config/ingestion-agent.yaml
```

### Full example

```yaml
bigpanda_jobs:
  enabled: true

  # When cric_path points to an existing cric_pandaqueues.json file the queue
  # names are read from the JSON keys and the 'queues' list is ignored.
  cric_path: /cvmfs/atlas.cern.ch/repo/sw/local/etc/cric_pandaqueues.json

  # Fallback list — used only when cric_path is absent or the file does not exist.
  queues:
    - SWT2_CPB
    - BNL

  # Cap the number of queues processed per cycle.  0 = no limit.
  max_queues: 0

  cycle_interval_s: 1800      # 30 minutes
  inter_queue_delay_s: 60     # 1 minute between queues

duckdb_path: "jobs.duckdb"
tick_interval_s: 1.0
```

### Queue discovery via CRIC

In production, ATLAS has hundreds of computing queues. Listing them all manually
in the YAML is impractical. The recommended approach is to point `cric_path` at
the `cric_pandaqueues.json` file maintained by the CRIC script on CVMFS:

```
/cvmfs/atlas.cern.ch/repo/sw/local/etc/cric_pandaqueues.json
```

The top-level keys of that JSON object are the PanDA queue names. When the file
exists the `queues` list is silently ignored. If the file cannot be read (missing
CVMFS mount, I/O error, malformed JSON), a warning is logged and the script falls
back to the `queues` list — no crash.

Because the full CRIC file contains ~700 queues, downloading all of them in one
cycle can take many hours with the default 60-second inter-queue delay. Use
`max_queues` (YAML) or `--max-queues` (CLI) to cap the number of queues
processed per cycle.

### `bigpanda_jobs` options

| Key | Default | Description |
|---|---|---|
| `enabled` | `true` | Enable or disable the BigPanda jobs fetcher entirely |
| `cric_path` | *(none)* | Path to `cric_pandaqueues.json`. When set and the file exists, queue names are read from its keys and `queues` is ignored |
| `queues` | `["SWT2_CPB", "BNL"]` | Fallback list of computing-site queue names to poll (used only when `cric_path` is absent or file is missing) |
| `max_queues` | `0` | Maximum queues to process per cycle. `0` means no limit — all discovered queues are polled |
| `cycle_interval_s` | `1800` | Minimum seconds between full polling cycles |
| `inter_queue_delay_s` | `60` | Seconds to sleep between consecutive queue downloads |

### Top-level options

| Key | Default | Description |
|---|---|---|
| `duckdb_path` | `":memory:"` | DuckDB file path, or `":memory:"` for an ephemeral in-memory database |
| `tick_interval_s` | `1.0` | Seconds between `tick()` calls in the run loop |
| `sources` | `[]` | List of generic file/URL sources (see below) |

### Generic `sources` entries

Each entry under `sources` supports:

| Key | Description |
|---|---|
| `name` | Unique source identifier (used as the table name prefix) |
| `type` | Logical type label (e.g. `cric_queue_data`) |
| `mode` | `file` or `url` |
| `path` | File path (required when `mode: file`) |
| `url` | HTTP/HTTPS URL (required when `mode: url`) |
| `interval_s` | Minimum seconds between fetches for this source |

---

## Running the script

### One-shot (single tick)

```bash
bamboo-ingestion --config path/to/ingestion-agent.yaml --once
```

Downloads all configured queues back-to-back (inter-queue delay suppressed) and exits. Useful for an initial data pull, cron-based scheduling, or debugging.

### Long-running daemon

```bash
bamboo-ingestion --config path/to/ingestion-agent.yaml
```

Loops indefinitely, calling `tick()` every `tick_interval_s` seconds. The BigPanda jobs cycle fires at most once per `cycle_interval_s`. Stop with Ctrl-C or SIGTERM — both trigger a clean shutdown.

### All command-line options

| Option | Default | Description |
|---|---|---|
| `--config`, `-c` | `src/.../ingestion-agent.yaml` | Path to the YAML configuration file |
| `--once` | off | Run a single tick then exit. Inter-queue delay is suppressed so all queues download back-to-back |
| `--inter-queue-delay SECONDS` | *(from YAML)* | Override `inter_queue_delay_s` at runtime without editing the config file. Set to `0` to disable the delay entirely, e.g. during debugging |
| `--max-queues N` | *(from YAML)* | Override `max_queues` at runtime. Set to `0` to process all available queues |
| `--log-file PATH` | `ingestion-agent.log` | Rotating log file (10 MB × 5 backups). Pass `""` to disable file logging |
| `--log-level LEVEL` | `INFO` | Minimum log level for both console and file output. One of `DEBUG`, `INFO`, `WARNING`, `ERROR` |

### Debugging tips

To do a quick test run against the full CRIC queue list without editing the config file — process only the first 10 queues with no inter-queue delay:

```bash
bamboo-ingestion \
  --config src/bamboo_mcp_services/resources/config/ingestion-agent.yaml \
  --once \
  --max-queues 10 \
  --inter-queue-delay 0 \
  --log-level DEBUG
```

The `--once` flag suppresses the inter-queue delay on its own, but `--inter-queue-delay 0` is useful in daemon mode when you want to observe back-to-back queue downloads without the default 60-second pause.

---

## Scripts

The `scripts/` directory contains standalone utility scripts that can be run directly with Python — no entry-point registration required.

### `dump_ingestion_db.py` — inspect the database from the command line

Dumps the contents of a `jobs.duckdb` file to stdout. Useful for quickly checking what the script has collected without opening a DuckDB shell.

```bash
# Show the first 10 rows of every table (default):
python scripts/dump_ingestion_db.py

# Use a specific database file and show 25 rows per table:
python scripts/dump_ingestion_db.py --db path/to/jobs.duckdb --limit 25

# Show only the jobs table, filtered to one queue:
python scripts/dump_ingestion_db.py --table jobs --queue SWT2_CPB

# JSON output (one object per line) — useful for piping to jq:
python scripts/dump_ingestion_db.py --table jobs --queue BNL --format json | jq '.pandaid, .jobstatus'

# Just the row counts for every table:
python scripts/dump_ingestion_db.py --count

# Print the column names and types (no row data):
python scripts/dump_ingestion_db.py --schema-only
```

#### All options

| Option | Default | Description |
|---|---|---|
| `--db PATH` | `jobs.duckdb` | Path to the DuckDB file |
| `--table`, `-t` | *(all)* | Restrict output to one table: `jobs`, `selectionsummary`, `errors_by_count`, or `snapshots` |
| `--queue`, `-q` | *(all queues)* | Filter rows to a specific `_queue` value (ignored for `snapshots`) |
| `--limit`, `-n` | `10` | Maximum rows per table. Pass `0` for no limit |
| `--format`, `-f` | `table` | `table` for aligned columns, `json` for newline-delimited JSON objects |
| `--schema-only` | off | Print column names and types instead of row data |
| `--count` | off | Print only the total row count per table |

---

## Querying the database

The DuckDB file can be opened directly by AskPanDA, a Jupyter notebook, or the `duckdb` CLI. The schema is guaranteed by `apply_schema()`, which is called automatically at script startup.

### DuckDB CLI

```bash
duckdb jobs.duckdb
```

```sql
-- Recent jobs for a queue
SELECT pandaid, jobstatus, durationsec, cpuefficiency
FROM jobs
WHERE _queue = 'SWT2_CPB'
ORDER BY _fetched_utc DESC
LIMIT 20;

-- Job status breakdown from the latest summary
SELECT field, list_json
FROM selectionsummary
WHERE _queue = 'SWT2_CPB';

-- Top errors for a queue
SELECT codename, codeval, count, error_desc_text
FROM errors_by_count
WHERE _queue = 'BNL'
ORDER BY count DESC;

-- Introspect the full jobs schema
DESCRIBE jobs;
```

### From Python (AskPanDA / Bamboo)

```python
import duckdb
from bamboo_mcp_services.common.storage.schema import apply_schema

conn = duckdb.connect("jobs.duckdb", read_only=True)
apply_schema(conn)  # no-op if tables already exist; ensures schema is present

df = conn.execute(
    "SELECT * FROM jobs WHERE _queue = ? ORDER BY _fetched_utc DESC",
    ["SWT2_CPB"],
).df()
```

---

## Data freshness and upsert semantics

| Table | Upsert strategy | Key |
|---|---|---|
| `jobs` | `INSERT OR REPLACE` — updates existing row | `pandaid` |
| `selectionsummary` | Delete-then-insert per queue per cycle | `_queue` |
| `errors_by_count` | Delete-then-insert per queue per cycle | `_queue` |

The `jobs` table accumulates rows across cycles: a job that appeared in the last cycle and is no longer returned by the API (e.g. it left the 1-hour window) will remain in the table with its last-known state. If you need a "current snapshot only" view, filter on `_fetched_utc`:

```sql
SELECT *
FROM jobs
WHERE _queue = 'SWT2_CPB'
  AND _fetched_utc = (SELECT MAX(_fetched_utc) FROM jobs WHERE _queue = 'SWT2_CPB');
```

---

## Architecture

```
IngestionAgent
├── _start_impl()          — opens DuckDB, initialises BigPandaJobsFetcher
├── _tick_impl()
│   ├── generic sources    — file/URL sources, each with their own interval
│   └── BigPandaJobsFetcher.run_cycle()
│       ├── interval check (skip if < cycle_interval_s since last run)
│       └── for each queue (logs progress: "processing queue 'X' (N/total)"):
│           ├── GET https://bigpanda.cern.ch/jobs/?computingsite=<Q>&json&hours=1
│           ├── _upsert_jobs()        → jobs table
│           ├── _insert_summary()     → selectionsummary table
│           ├── _insert_errors()      → errors_by_count table      (all three in one transaction)
│           └── sleep inter_queue_delay_s  (skipped after last queue)
└── _stop_impl()           — releases fetcher and DuckDB connection
```

Key modules:

| Module | Purpose |
|---|---|
| `agents/ingestion_agent/agent.py` | Agent lifecycle, config dataclasses |
| `agents/ingestion_agent/bigpanda_jobs_fetcher.py` | BigPanda download loop and DB persistence |
| `agents/ingestion_agent/cli.py` | CLI entry point (`bamboo-ingestion`) |
| `common/storage/schema.py` | DuckDB DDL — single source of truth for all table schemas |
| `common/storage/duckdb_store.py` | Low-level DuckDB helpers (snapshots table, generic write) |
| `common/panda/source.py` | File and URL fetching with content hashing |
| `scripts/dump_ingestion_db.py` | Standalone CLI tool for inspecting the database |

---

## CI and testing

```bash
pytest tests/agents/ingestion_agent/ -v
```

The test suite covers:

- Schema idempotency (`apply_schema` called twice does not raise).
- All three tables are created by `apply_schema`.
- `jobs` primary-key upsert — a second insert for the same `pandaid` replaces, not duplicates.
- Full cycle with mocked HTTP response — jobs, summary, and errors all land in the correct tables.
- Upsert deduplication over two consecutive cycles.
- `selectionsummary` and `errors_by_count` are replaced (not accumulated) on each cycle.
- Empty API response does not raise.
- Inter-queue `time.sleep` is called exactly once between two queues, and not at all after the last queue.
- A failing queue does not prevent remaining queues from being fetched.
- **Transaction safety** — all three tables are updated atomically; a simulated mid-write failure triggers ROLLBACK, leaving the previous committed data intact with no partial updates.

HTTP calls are mocked with `unittest.mock.patch` so no network access is required during tests.

---

## Relationship to AskPanDA / Bamboo

The `jobs.duckdb` file is the handoff point between the ingestion script and the Bamboo / AskPanDA plugin. The plugin opens the file in **read-only** mode and queries the typed tables directly. The schema module (`schema.py`) can be copied into or imported by the plugin to guarantee the expected columns are present before issuing queries.
