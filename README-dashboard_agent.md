# dashboard

The `dashboard` serves a live, dark-themed web UI for monitoring Bamboo MCP Services.  It starts a lightweight [FastAPI](https://fastapi.tiangolo.com/) / [uvicorn](https://www.uvicorn.org/) HTTP server in a background daemon thread and exposes REST endpoints that the single-page dashboard polls on a configurable auto-refresh interval.

No additional ingestion or processing is performed — the dashboard is **read-only** and queries the DuckDB databases written by the other scripts (`jobs.duckdb` from the ingestion script, `cric.duckdb` from the CRIC script).

---

## Quick start

```bash
# Start the dashboard (reads jobs.duckdb and cric.duckdb in the current directory):
bamboo-dashboard

# Customise database paths:
bamboo-dashboard --jobs-db /data/jobs.duckdb --cric-db /data/cric.duckdb

# Start, verify the server is up, print the URL, then exit:
bamboo-dashboard --once

# Custom port and faster refresh:
bamboo-dashboard --port 9090 --refresh 10
```

Open `http://localhost:8080` (or whatever `--host` / `--port` you set) in any browser.

Stop the server with **Ctrl-C** or `kill -TERM <pid>`.

---

## What the dashboard shows

The single-page dashboard is divided into several panels that refresh automatically:

**Database status bar (header)** — live indicator dots for both DuckDB files.  Green = reachable, red = error.  Shows the timestamp of the last BigPanda fetch.

**Job summary** — total job count and a breakdown by status (`finished`, `failed`, `running`, `pending`, …).  Rendered as a doughnut chart alongside numeric badges.

**Jobs by queue** — a horizontal bar chart of the top 20 queues ranked by job count.

**Queue status** — a doughnut chart of CRIC-registered queues grouped by status (`online`, `offline`, `test`, …).

**Queue cloud distribution** — a bar chart of the top 15 clouds by number of registered queues.

**Error summary table** — the 25 most frequent error codes (from `errors_by_count`), with columns for error name, code, diagnostic message, and total occurrence count.

---

## REST API

The dashboard consumes these endpoints, which are also accessible directly:

| Endpoint | Description |
|---|---|
| `GET /` | The dashboard HTML page |
| `GET /api/config` | Active configuration (paths, refresh interval) |
| `GET /api/status` | Liveness check for both DuckDB files |
| `GET /api/jobs/summary` | Total job count, breakdown by status, last fetch timestamp |
| `GET /api/jobs/by_queue` | Top 20 queues by job count |
| `GET /api/errors` | Top 25 error codes from `errors_by_count` |
| `GET /api/queues` | Queue counts by status and cloud from `queuedata` |

All data endpoints return JSON and respond with HTTP 503 (and an `{"error": "…"}` body) if the relevant database cannot be queried.

---

## CLI reference

```
bamboo-dashboard [OPTIONS]
```

| Flag | Default | Description |
|---|---|---|
| `--jobs-db PATH` | `jobs.duckdb` | Path to the ingestion script's DuckDB file |
| `--cric-db PATH` | `cric.duckdb` | Path to the CRIC script's DuckDB file |
| `--host ADDR` | `0.0.0.0` | HTTP server bind address |
| `--port PORT` | `8080` | HTTP server port |
| `--refresh SECONDS` | `30` | Dashboard client auto-refresh interval |
| `--tick-interval SECONDS` | `15.0` | Health-check interval (server thread liveness) |
| `--log-file PATH` | `dashboard-agent.log` | Rotating log file; pass `''` to disable |
| `--log-level LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, or `ERROR` |
| `--once` | — | Start, verify server is up, print URL, then exit |

---

## Running under the supervisor

Add the following block to your `supervisor-agent.yaml` to have the supervisor manage the dashboard alongside the other scripts:

```yaml
- name: dashboard
  mode: daemon
  command: bamboo-dashboard
  args:
    - "--jobs-db"
    - "/abs/path/to/jobs.duckdb"
    - "--cric-db"
    - "/abs/path/to/cric.duckdb"
    - "--port"
    - "8080"
    - "--log-file"
    - "dashboard-agent.log"
```

The supervisor will restart the dashboard automatically if it exits unexpectedly.

---

## Design notes

**Read-only DuckDB connections** — every query opens a `read_only=True` connection, executes the query, and closes immediately.  This is safe to run alongside a writing script because DuckDB's MVCC ensures consistent committed snapshots for read-only connections.

**Background thread model** — uvicorn runs in a daemon thread started by `_start_impl`.  The script's `_tick_impl` simply verifies the thread is still alive; if it has died, a `RuntimeError` is raised so the supervisor can restart the process.

**Static HTML, no build step** — the dashboard UI is a self-contained `static/index.html` file served directly by FastAPI.  It imports Tailwind CSS and Chart.js from CDNs.  No Node.js, bundler, or build pipeline is required.

**`__REFRESH_INTERVAL__` substitution** — the HTML template contains the literal string `__REFRESH_INTERVAL__` which is replaced at serve time with the configured `--refresh` value before sending the response.  This keeps the refresh interval as a single source of truth in the Python config.

**Graceful shutdown** — `_stop_impl` sets `uvicorn.Server.should_exit = True` and joins the thread with a 5-second timeout before returning.  SIGTERM is forwarded to `script.stop()` by the CLI signal handler.
