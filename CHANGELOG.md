# Changelog

All notable changes to this project will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Fixed

#### Dashboard agent — uvicorn startup race and misleading URL in logs

Two bugs in `agents/dashboard_agent/agent.py` and `cli.py` that caused the
dashboard to appear unreachable after a fresh start:

- **Startup race condition** (`agent.py` `_start_impl`): `start()` returned
  immediately after launching the uvicorn background thread, before uvicorn
  had bound its socket.  A `--once` probe or an early browser request could
  therefore hit a port that was not yet listening.  `_start_impl` now polls
  `uvicorn.Server.started` in a 50 ms loop for up to 10 seconds before
  returning; if the server does not become ready in time a `RuntimeError` is
  raised (typically indicating the port is already in use).

- **`0.0.0.0` logged as the browser URL** (`agent.py`, `cli.py`): the startup
  log message and `--once` output both printed `http://0.0.0.0:<port>`, which
  is a bind address understood by the OS but **not** a valid URL for a browser.
  Both now log `http://localhost:<port>`.

### Added

#### Atomic storage updates — zero-downtime reads during every write cycle

All three storage layers used by Bamboo MCP Services now guarantee that
concurrent readers (e.g. Bamboo MCP tools querying live data) **never observe
a gap, a partial write, or a torn state** while an agent is updating its data.

---

##### ChromaDB — blue/green slot rotation (`document-monitor-agent`)

The `document-monitor-agent` now maintains two physical ChromaDB collections
per logical collection name (the *blue* and *green* slots, named
`<collection>__a` and `<collection>__b`).  Readers always address the currently
live slot; each update cycle writes into the idle slot and then promotes it
atomically:

1. The idle slot is **deleted and recreated from scratch** before any vectors
   are written.  This clears any embedding dimension locked in by a previous
   cycle, so a changed embedder model can never cause a dimension-mismatch
   error.
2. All chunks are embedded and written into the idle slot (no impact on
   readers).
3. A routing sidecar file (`<chroma-dir>/collection_routing.json`) is updated
   via `os.replace` — a POSIX atomic operation — to point the logical name at
   the newly-built slot.
4. The old live slot is deleted.

Between steps 2 and 4 the old slot remains fully queryable.  There is no
window where the collection is empty or partially filled.

**New class `CollectionRouter`** in
`agents/document_monitor_agent/storage.py` manages the sidecar and slot
selection.  It is crash-safe: a stale sidecar from a previous crash is simply
reloaded on the next start; a corrupt or missing sidecar defaults to slot `__a`.

**`ChromaWrapper.create_collection`** now calls `client.create_collection`
rather than `client.get_or_create_collection`.  The distinction is critical:
`get_or_create` reattaches to an existing collection and inherits its locked
embedding dimension; `create_collection` always starts with no dimension
constraint.

The `_health_details` report now includes a `chroma_live_slot` field showing
which physical slot is currently live (e.g. `atlas_docs__a`).

---

##### DuckDB — shadow-table rename (`cric-agent`, `ingestion-agent`, `duckdb_store`)

A new free function **`atomic_swap_table(conn, staging_name, live_name)`** in
`common/storage/duckdb_store.py` atomically promotes a fully-populated staging
table to become the live table using `ALTER TABLE RENAME`, a metadata-only
operation in DuckDB.  The entire rename is wrapped in a single short
transaction (three DDL statements, no data movement) so readers either see the
complete old table or the complete new one.

The previous pattern — `BEGIN; DROP TABLE; CREATE TABLE; INSERT many rows;
COMMIT` — had the DROP inside the transaction, which still creates a visible
gap for readers that start a fresh snapshot after the DROP but before the
COMMIT.  The rename pattern eliminates that gap entirely.

**`cric_fetcher._load`**: replaced the DROP-inside-transaction block with a
shadow-swap sequence: build into `queuedata_staging` (outside any transaction),
then call `atomic_swap_table` to flip `queuedata_staging` → `queuedata`
atomically.  The heavy bulk-insert work now happens entirely outside the
transaction.

**`DuckDBStore.write_table(overwrite=True)`**: same pattern — rows are written
into `<table>_staging` first, then promoted via `atomic_swap_table`.

**`schema._migrate_composite_pk`**: the bare `DROP TABLE` in the one-time
migration is now wrapped in `BEGIN/COMMIT/ROLLBACK`.

`atomic_swap_table` also cleans up any stale `<live>_retiring` table left over
from a previous crash, so the function is safe to call repeatedly without
manual cleanup.

---

**New test file `tests/test_atomic_updates.py`** — 26 tests covering:

- `atomic_swap_table`: first-run (no live table), normal swap, stale-retiring
  recovery, rollback-on-error preserves live, repeated swaps.
- `DuckDBStore.write_table(overwrite=True)`: content correctness, concurrent
  reader continuity (file-backed DB, dedicated read connection).
- `CricQueuedataFetcher._load`: first load, reload replaces content, concurrent
  reader never sees zero rows, no staging table left behind, empty-data guard.
- `CollectionRouter`: slot defaults, sidecar persistence across instances,
  swap alternation, crash recovery, multi-collection independence, atomic
  `.tmp` handling.
- `ChromaWrapper.create_collection`: verifies the client method called,
  confirms `_ingest_file` issues delete-before-create on the idle slot.

**Changed files:**

- `src/bamboo_mcp_services/common/storage/duckdb_store.py` — new
  `atomic_swap_table` function; `write_table(overwrite=True)` rewritten to use
  shadow-swap.
- `src/bamboo_mcp_services/agents/cric_agent/cric_fetcher.py` — `_load`
  rewritten; `_create_table` renamed to `_create_staging_table`; `_insert_rows`
  gains a `table` parameter.
- `src/bamboo_mcp_services/common/storage/schema.py` — `_migrate_composite_pk`
  DROP wrapped in transaction.
- `src/bamboo_mcp_services/agents/document_monitor_agent/storage.py` — new
  `CollectionRouter` class; `ChromaWrapper.create_collection` fixed to call
  `client.create_collection`.
- `src/bamboo_mcp_services/agents/document_monitor_agent/agent.py` —
  `__init__` wires up `CollectionRouter`; `_ingest_file` rewritten with
  blue/green swap; `_health_details` adds `chroma_live_slot`.
- `tests/test_atomic_updates.py` — new, 26 tests.
- `README-document_monitor_agent.md` — updated design guarantees, re-ingestion,
  and operational guidance for the new storage layout.
- `README.md` — updated `document-monitor-agent` description and common
  pitfalls.

---

### Added (prior unreleased)

A new `dashboard-agent` serves a dark-themed, single-page web UI for monitoring
the Bamboo MCP Services system in real time.  It starts a
[FastAPI](https://fastapi.tiangolo.com/) / [uvicorn](https://www.uvicorn.org/)
HTTP server in a background daemon thread and exposes REST endpoints that the
dashboard polls on a configurable auto-refresh interval.

```bash
# Serve the dashboard (reads jobs.duckdb and cric.duckdb by default):
bamboo-dashboard

# Custom paths and port:
bamboo-dashboard --jobs-db /data/jobs.duckdb --cric-db /data/cric.duckdb --port 9090

# Start, verify the server is up, print the URL, then exit:
bamboo-dashboard --once
```

**Dashboard panels:**

| Panel | Data source |
|---|---|
| Database status bar (header) | `/api/status` — liveness indicator for both DuckDB files |
| Job summary (doughnut chart + badges) | `/api/jobs/summary` — total count and breakdown by status |
| Jobs by queue (bar chart) | `/api/jobs/by_queue` — top 20 queues by job count |
| Queue status (doughnut chart) | `/api/queues` — CRIC queue count by status |
| Cloud distribution (bar chart) | `/api/queues` — top 15 clouds by queue count |
| Error summary (table) | `/api/errors` — top 25 error codes from `errors_by_count` |

**Design highlights:**

- **Read-only DuckDB** — every endpoint opens a `read_only=True` connection per
  request and closes it immediately.  Safe to run alongside the writing agents;
  DuckDB's MVCC guarantees a consistent committed snapshot.
- **Background daemon thread** — uvicorn runs in a `threading.Thread(daemon=True)`.
  `_tick_impl` verifies the thread is still alive; a dead thread causes
  `RuntimeError` so the supervisor can restart the process.
- **No build step** — the dashboard UI is a self-contained `static/index.html`
  that imports Tailwind CSS and Chart.js from CDNs.
- **`__REFRESH_INTERVAL__` substitution** — the HTML template placeholder is
  replaced at serve time with the configured `--refresh` value.
- **Graceful shutdown** — `_stop_impl` sets `uvicorn.Server.should_exit = True`
  and joins the thread with a 5-second timeout.

**New files:**

- `src/bamboo_mcp_services/agents/dashboard_agent/__init__.py`
- `src/bamboo_mcp_services/agents/dashboard_agent/agent.py` — `DashboardConfig`,
  `DashboardAgent`, and `build_app()` with five route groups.
- `src/bamboo_mcp_services/agents/dashboard_agent/cli.py` — `bamboo-dashboard`
  entry point.
- `src/bamboo_mcp_services/agents/dashboard_agent/static/index.html` — the
  single-page monitoring dashboard.
- `tests/agents/dashboard_agent/__init__.py`
- `tests/agents/dashboard_agent/test_dashboard_agent.py` — 25 tests covering
  config defaults, agent lifecycle state transitions, thread-liveness checks,
  `__REFRESH_INTERVAL__` injection, all REST endpoints (success and DB-error
  paths), and health detail formatting.
- `README-dashboard_agent.md` — full operator guide.

**`pyproject.toml`** — added `bamboo-dashboard` entry point.

**`README.md`** — updated status table (`dashboard-agent` now ✅ Ready), added
"Run the dashboard agent" section to Getting started, added agent description
with key features, updated repository layout.

**`CLAUDE.md`** — added `dashboard-agent` to the agent table, run-instructions
block, repository layout, tests tree, and a new `dashboard_agent — key design
decisions` section.  Agent-creation checklist updated to cite `dashboard_agent`
as the template for HTTP-server agents.

---

### Added (prior unreleased)

#### `supervisor-agent` — single entry point for the full system

A new `supervisor-agent` manages all other Bamboo MCP Services agents as child
subprocesses.  Operators no longer need to start, monitor, or restart individual
agents manually.

```bash
bamboo-supervisor --config supervisor-agent.yaml
```

**Two operating modes, configurable per agent:**

* **`daemon`** — the agent process runs indefinitely.  The supervisor polls it
  every `health_poll_interval_s` seconds and restarts it if it exits.
  Rapid-restart loops trigger exponential back-off (5 s → 10 s → 20 s … capped
  at 300 s) to prevent hammering a failing dependency.

* **`scheduled`** — a short-lived `--once` process is launched on a fixed
  `interval_s` cadence.  The supervisor waits for it to complete, records the
  exit code, then schedules the next run.  Runs that exceed `run_timeout_s` are
  killed and an error is logged.

**Default configuration** (in `supervisor-agent.yaml`) starts:

| Agent | Mode | Cadence |
|---|---|---|
| `cric` | daemon | continuous |
| `ingestion` | daemon | continuous (waits for `cric.duckdb`) |
| `github-sync` | scheduled | every hour |
| `document-monitor` | scheduled | every 10 minutes |

**Dependency ordering** — the `depends_on_file` / `depends_timeout_s` config
keys allow an agent to wait for a file written by another agent before starting.
Used to ensure `cric.duckdb` is populated before the ingestion agent reads it.

**Operational features:**

* `bamboo-supervisor --status` prints a JSON config summary and exits — no
  processes are started.  Useful for verifying configuration on a new machine.
* `bamboo-supervisor --once` starts all agents, runs one health-poll tick
  (dispatching due scheduled jobs), logs the health report as JSON, then stops
  cleanly.
* Rotating log file (`supervisor-agent.log`, 10 MB × 5 backups); each managed
  agent continues writing to its own separate log.
* Clean SIGTERM / SIGINT propagation: the supervisor forwards SIGTERM to all
  children and waits `stop_timeout_s` before escalating to SIGKILL.

**Future extension point** — a lightweight HTTP health endpoint
(`GET /health → JSON`) is planned for remote deployments.  The data structure
is already fully defined in `SupervisorAgent.health()`; the HTTP layer will be
a thin wrapper added in a future release.  See `TODO: HTTP health endpoint`
comments in `agent.py`.

**New files:**

* `src/bamboo_mcp_services/agents/supervisor_agent/__init__.py`
* `src/bamboo_mcp_services/agents/supervisor_agent/scheduler.py` — per-agent
  scheduling state (`DaemonState`, `ScheduledState`, `AgentConfig`).  Pure
  Python with no subprocess calls, making it fully unit-testable in isolation.
* `src/bamboo_mcp_services/agents/supervisor_agent/agent.py` — `SupervisorAgent`
  implementation.
* `src/bamboo_mcp_services/agents/supervisor_agent/cli.py` — `bamboo-supervisor`
  entry point.
* `src/bamboo_mcp_services/resources/config/supervisor-agent.yaml` — default
  config covering all four production agents.
* `tests/agents/supervisor_agent/test_supervisor_agent.py` — 22 tests covering
  daemon start/restart/back-off/stop, scheduled dispatch/skip/timeout/advance,
  dependency waiting, config loading, and the `--status` / `--once` CLI flags.
  All subprocess interaction is mocked.
* `README-supervisor-agent.md` — full operator guide.

**`pyproject.toml`** — added `bamboo-supervisor` entry point.

**`README.md`** — updated status table (supervisor-agent now ✅ Ready), added
"Run the supervisor" section to Getting started, updated repository layout,
added link to `README-supervisor-agent.md`.

### Added

#### Configurable ChromaDB collection name in `document-monitor-agent`

The `bamboo-document-monitor` CLI now accepts a `--collection` flag that sets
the ChromaDB collection name at runtime.  Previously the name was hardcoded as
`"atlas_docs"`, making it impossible to ingest separate document corpora into
distinct collections without modifying source code.

```bash
bamboo-document-monitor \
  --dir ../CGSim-RAG \
  --chroma-dir ../chromadb-cgsim \
  --collection cgsim_docs \
  --once
```

The default remains `"atlas_docs"` so existing invocations are unaffected.
When running multiple corpora, use a distinct `--collection` **and** a distinct
`--checkpoint-file` per invocation to keep file state fully isolated.

Implementation: `build_parser()` gains a `--collection` argument; `_build_agent()`
passes `args.collection` to `DocumentMonitorAgent(name=...)`.

#### Generic git repository support in `github-doc-sync-agent`

The `github-doc-sync-agent` can now sync documentation from **any publicly-accessible
git repository**, not just GitHub or GitHub wikis.  This includes GitLab,
FramaGit, Bitbucket, Gitea, and any other host that exposes a public HTTPS clone URL.

To enable, set `git: true` and provide a `clone_url` in the repo entry:

```yaml
- name: simgrid/simgrid
  git: true
  clone_url: https://framagit.org/simgrid/simgrid.git
  branch: master
  destination: ./data/simgrid/raw
  normalized_destination: ./data/simgrid/normalized
  within_hours: 168
  include_patterns:
    - "docs/source/*.rst"
  normalize_for_rag: true
```

The `name` field is used for logging, directory naming, and RAG metadata only
— it does not need to match an actual GitHub owner/repo path.  The `branch`
field is respected and passed as `-b` to `git clone`.

Implementation details:

- New `git: bool = False` and `clone_url: Optional[str] = None` fields on `RepoConfig`.
- New `sync_git_repo()` function in `github_markdown_sync.py` that clones the
  repository via `clone_url`, reads the HEAD SHA and committer datetime, applies
  the same `within_hours` and SHA-unchanged skip logic as the other paths, and
  copies and normalises matching files identically to `sync_wiki_repo()`.
- `sync_repo()` dispatch order: `wiki=True` → `sync_wiki_repo()`, `git=True` →
  `sync_git_repo()`, otherwise → GitHub REST API path.
- `load_config()` and `_load_repo_configs()` (CLI) both read the new fields,
  defaulting to `False`/`None` when absent.
- Generic git clones do not count against the GitHub REST API rate limit.
- 9 new tests covering dispatch routing, missing `clone_url` validation, branch
  flag passing, file copy and normalisation, SHA-unchanged skip, and
  `load_config` YAML parsing.

#### GitHub wiki support in `github-doc-sync-agent`

The `github-doc-sync-agent` can now sync **GitHub wiki repositories** in
addition to regular repositories.  GitHub wikis are not accessible via the
REST API, so a `git clone --depth 1` path is used instead.

To enable, add `wiki: true` to any repo entry in the YAML config and use
`owner/repo.wiki` as the `name`:

```yaml
- name: PanDAWMS/pilot3.wiki
  wiki: true
  destination: ../raw
  normalized_destination: ../RAG
  within_hours: 10
  include_patterns:
    - "*.md"
  normalize_for_rag: true
```

Implementation details:

- New `wiki: bool = False` field on `RepoConfig`.
- New `sync_wiki_repo()` function in `github_markdown_sync.py` that clones the
  wiki via `https://github.com/{owner}/{repo}.wiki.git`, reads the HEAD SHA and
  committer datetime with `git rev-parse HEAD` / `git log -1 --format=%cI`,
  applies the same `within_hours` and SHA-unchanged skip logic as the REST
  path, copies matching files to `destination`, and optionally normalises them
  into `normalized_destination`.
- `sync_repo()` now dispatches to `sync_wiki_repo()` when `cfg.wiki` is
  `True`, leaving the existing REST API path completely unchanged.
- `load_config()` and `_load_repo_configs()` (CLI) both read the new `wiki`
  field, defaulting to `False` when absent.
- The `branch` config key is silently ignored for wiki repos — `git clone`
  always fetches the default branch.
- Wiki clones do not count against GitHub's REST API rate limit.
- 8 new tests covering dispatch, URL construction, file copy and
  normalisation, `within_hours` skip on second run, and `load_config` parsing.

---

## [1.0.0] — 2026-04-08

First stable release.  All four agents (`ingestion`, `cric`, `document-monitor`,
`github-doc-sync`) are production-ready.  This release focuses on correctness
under concurrent read/write access, operational observability, and release
tooling.

### Fixed

#### Concurrency — DuckDB torn-read protection

All database write operations that involve multiple SQL statements are now
wrapped in explicit `BEGIN` / `COMMIT` / `ROLLBACK` transactions.  Before this
fix, a query arriving from AskPanDA (via the Bamboo MCP tool) during a write
cycle could observe a missing table, an empty table, or a partially-inserted
result.

- **`cric_fetcher._load()`** — the full `DROP TABLE → CREATE TABLE → INSERT`
  sequence for `queuedata` is now a single atomic transaction.  Concurrent
  readers always see either the previous complete snapshot or the new one.
- **`DuckDBStore.write_table(overwrite=True)`** — same fix applied to the
  generic overwrite path used by the ingestion agent's source history tables.
- **`BigPandaJobsFetcher._fetch_and_persist()`** — the three-table write
  (`jobs` upsert + `selectionsummary` replace + `errors_by_count` replace) for
  each queue is now a single transaction.  A reader can never observe a state
  where some tables have been updated for a cycle and others have not.

#### Concurrency — ChromaDB staging swap

`DocumentMonitorAgent._ingest_file()` previously deleted old chunks from the
live ChromaDB collection and then inserted new ones, leaving a window where
the document was invisible to concurrent queries.  The update path now uses an
atomic staging swap:

1. Write all new chunks into a temporary `<name>__staging` collection.
2. Delete the old chunks from the live collection.
3. Insert from staging into the live collection.
4. Drop the staging collection (in a `finally` block).

If the staging write fails the live collection is never touched.  If the swap
step fails the old chunks remain visible.

### Added

#### `scripts/bump_version.py` — release versioning script

A new script for bumping the version string across all relevant files in one
command:

```bash
python scripts/bump_version.py 0.1.0 1.0.0
```

Validates both version strings against PEP 440, reports each file updated, and
exits non-zero on any failure — safe to run in CI.  After a successful bump the
script prints a reminder to reinstall the package so agents pick up the new
version at runtime:

```
IMPORTANT: reinstall the package so agents report the new version at runtime:
    pip install -e .
```

#### `common/cli.py` — shared startup banner

A new `log_startup_banner(logger, prog)` helper in
`bamboo_mcp_services.common.cli` emits a consistent startup line on every
agent launch:

```
bamboo-cric  version=1.0.0  python=3.12.3
```

The version is resolved at runtime from the installed package metadata
(`importlib.metadata`) so it always reflects the version in `pyproject.toml`
without requiring a hardcoded constant.

Previously three of the four CLIs each contained an inline 9-line copy of this
logic, and `bamboo-document-monitor` had no version logging at all.  The helper
replaces all four with a single call.

#### Per-queue progress logging in `BigPandaJobsFetcher`

The ingestion agent now logs a progress line before fetching each queue,
showing the current position in the cycle:

```
BigPandaJobsFetcher: processing queue 'BNL' (10/230)
```

This makes it straightforward to monitor long cycles (e.g. 230 queues × 60 s
inter-queue delay ≈ 4 hours) and to identify which queue a failure or slowdown
is associated with.

#### New test coverage — 16 tests across 3 files

| File | What is tested |
|---|---|
| `tests/agents/cric_agent/test_cric_agent.py` | Transaction safety: successful load gives complete table; failed load ROLLBACK preserves previous snapshot; table never absent after a write |
| `tests/agents/ingestion_agent/test_bigpanda_jobs_fetcher.py` | Transaction safety: all three tables updated atomically; failed mid-write ROLLBACK leaves baseline intact |
| `tests/test_duckdb_store.py` *(new)* | `write_table` append and overwrite modes; ROLLBACK on insert failure preserves previous data; table existence after a failed overwrite; `record_snapshot` round-trips |

### Changed

- `bamboo-document-monitor` CLI now uses `log_startup_banner` and therefore
  logs version and Python information on startup, matching the other three
  agents.
- `DuckDBStore.write_table(overwrite=False)` is unchanged — the append path
  involves no DROP and was already safe.

### Notes for operators

**AskPanDA / Bamboo MCP read connections** — the transaction fixes above protect
against torn reads caused by the write process, but for full safety the MCP
query tool should open DuckDB files with `read_only=True`:

```python
conn = duckdb.connect(database="cric.db", read_only=True)
conn = duckdb.connect(database="jobs.duckdb", read_only=True)
```

DuckDB enforces a single-writer policy at the file level.  A second connection
opened with `read_only=False` (the default) while the agent holds the write
connection will either block or raise `IOException: Database is already open`.
`read_only=True` connections are explicitly allowed to coexist with one writer.

---

## [0.1.0] — initial development release

All four agents (`ingestion`, `cric`, `document-monitor`, `github-doc-sync`)
implemented and passing the full test suite.  Not yet recommended for
production use.
