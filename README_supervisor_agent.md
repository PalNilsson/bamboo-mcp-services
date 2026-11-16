# supervisor

The `supervisor` is the single entry point for running all Bamboo MCP
Services together.  It starts every other script as a child subprocess, monitors
them, and keeps them running — so operators do not need to know about individual
scripts or their command-line flags.

---

## Quick start

```bash
# Activate the environment
conda activate bamboo-mcp-services

# Copy and edit the config (first time only)
cp src/bamboo_mcp_services/resources/config/supervisor-agent.yaml ./supervisor-agent.yaml
$EDITOR supervisor-agent.yaml

# Start the supervisor (and all agents)
bamboo-supervisor --config supervisor-agent.yaml
```

Stop with **Ctrl-C** or `kill -TERM <pid>`.  All child processes receive SIGTERM
and are given 30 seconds to exit cleanly before SIGKILL is sent.

---

## How it works

The supervisor manages each script in one of two modes:

### Daemon mode

The script process runs indefinitely.  The supervisor polls it every
`health_poll_interval_s` seconds (default: 30 s).  If the process exits for any
reason, the supervisor restarts it.  A script that fails to *launch* — most
often because its command is not on `PATH` — is retried in exactly the same
way, so a missing entry point shows up as a repeating ERROR rather than a
single line followed by silence.

Repeated **fast failures** trigger exponential back-off.  An exit counts as a
fast failure when the process had been running for less than 60 s, or never
started at all.  The first fast failure is restarted at the next tick; each
further one in a row doubles the wait (5 s → 10 s → 20 s … up to 300 s, rounded
up to the next tick).  A run that lasts at least 60 s resets the count.  Each
exit is logged once, with what happened in words:

```
Daemon agent 'cric' (pid=1716) exited with code 3 after 3s — 2 fast failures in a row, back-off: next attempt in 5s.
```

### Scheduled mode

A short-lived `--once` process is launched on a fixed interval
(`interval_s`).  The supervisor does **not** wait for it: it records the exit
code on the first tick after the run finishes, then schedules the next run.
Daemons keep being monitored while a long run (a GitHub sync, an embedding
pass) is in progress.  If a run exceeds `run_timeout_s` (default:
`interval_s × 2`), the process is killed and the error is logged.  Both the
completion and the timeout are noticed at tick granularity, i.e. up to
`health_poll_interval_s` late.

Every scheduled script is due on the supervisor's first tick, so each runs
once immediately at startup rather than one interval later.

### Dependency ordering

A script can declare a file dependency with `depends_on_file`.  The supervisor
does not start the script until that file exists, or until `depends_timeout_s`
seconds (default: 120 s) have passed, after which it starts it anyway with a
warning.  A daemon waits only before its first launch; a scheduled script waits
before every run while the file is missing.

The wait does not block the supervisor.  The file is checked once per tick, so
the script starts up to `health_poll_interval_s` after the file appears, and
every other script is polled, restarted and dispatched as usual meanwhile.
(Earlier versions slept inside the loop for the whole wait.)  The health report
shows a waiting script's file under `waiting_for`.

No script in the default configuration needs a dependency: the ingestion
script reads the CRIC queue list from the CVMFS JSON file itself, not from the
CRIC script's database.

### Startup sequence (default config)

1. `bamboo-cric` starts immediately as a daemon — creates `cric.duckdb`.
2. `bamboo-ingestion` starts immediately as a daemon.
3. `bamboo-github-sync` is registered as a scheduled job (hourly); it runs for
   the first time on the next tick.
4. `bamboo-document-monitor` is registered as a scheduled job (every 10 min).

---

## Configuration

The default config is bundled at:

```
src/bamboo_mcp_services/resources/config/supervisor-agent.yaml
```

Copy it to your working directory and edit as needed.  All paths in `command`
lists are resolved relative to the working directory where `bamboo-supervisor`
is launched.

### Top-level keys

| Key | Default | Description |
|---|---|---|
| `health_poll_interval_s` | `30` | How often the supervisor checks daemon health and dispatches due scheduled jobs. |
| `stop_timeout_s` | `30` | Seconds to wait for graceful shutdown before SIGKILL. |
| `log_file` | `supervisor-agent.log` | Rotating log file (override with `--log-file`). |
| `log_level` | `INFO` | Log verbosity (override with `--log-level`). |

### Per-script keys

| Key | Required | Description |
|---|---|---|
| `name` | ✅ | Unique identifier used in logs and health reports. |
| `mode` | ✅ | `daemon` or `scheduled`. |
| `command` | ✅ | Argument list passed verbatim to the OS (YAML list). |
| `enabled` | — | `true` (default) or `false` to skip without removing the entry. |
| `interval_s` | ✅ (scheduled) | Seconds between one-shot runs. |
| `depends_on_file` | — | Path that must exist before this script is started. |
| `depends_timeout_s` | — | Seconds to wait for `depends_on_file` (default: 120). |
| `run_timeout_s` | — | Kill scheduled one-shots that run longer than this (default: `interval_s × 2`). |

### Example: adding a new script

```yaml
agents:
  # ...existing agents...

  - name: my-new-agent
    enabled: true
    mode: scheduled
    interval_s: 1800          # every 30 minutes
    command:
      - bamboo-my-new-agent
      - --config
      - src/bamboo_mcp_services/resources/config/my-new-agent.yaml
      - --once
      - --log-file
      - my-new-agent.log
```

### Disabling a script temporarily

Set `enabled: false`:

```yaml
  - name: github-sync
    enabled: false
    # ...rest of entry unchanged...
```

---

## CLI reference

```
bamboo-supervisor [OPTIONS]

Options:
  -c, --config PATH     YAML config file (default: supervisor-agent.yaml)
  --log-file PATH       Rotating log file (default: supervisor-agent.log)
  --log-level LEVEL     DEBUG / INFO / WARNING / ERROR (default: INFO)
  --once                Smoke test: start, check, run scheduled scripts once, stop
  --once-settle-s SECS  With --once: how long daemons run before the check (default: 15)
  --heartbeat-file PATH Heartbeat written after every tick (default: supervisor-heartbeat.json)
  --status              Check the running supervisor's heartbeat; exit 0 if fresh, 1 if not
  --max-age SECS        With --status: oldest acceptable heartbeat (default: 120)
```

### `--status`

Reports whether a running supervisor is alive and its loop is making progress.
The running supervisor rewrites a heartbeat file (`--heartbeat-file`, default
`supervisor-heartbeat.json` in the working directory) after every tick, with
its full health report.  `--status` reads that file and exits:

* **0** — the heartbeat is at most `--max-age` seconds old (default 120) and
  the supervisor is in state `running`;
* **1** — the file is missing, malformed, stale, or was written by a
  supervisor that has stopped (a clean stop writes a final heartbeat in state
  `stopped`).

```bash
bamboo-supervisor --status --heartbeat-file /data/bamboo/supervisor/heartbeat.json
```

```json
{
  "age_s": 12.4,
  "heartbeat": {
    "health": {
      "details": {"agents": {"cric": {"mode": "daemon", "pid": 1716, "restarts": 0, ...}, ...}},
      "state": "running",
      ...
    },
    "schema_version": 1,
    "supervisor_pid": 1702,
    "written_at_epoch": 1794573600.1,
    "written_at_utc": "2026-11-13T10:40:00.100000+00:00"
  },
  "ok": true,
  "reason": "supervisor running, heartbeat 12s old"
}
```

The verdict is about the supervisor loop only.  The agents' states are in the
report, but an agent in back-off never fails the check: the supervisor
restarts agents itself, and a liveness probe that restarted the whole
container for one agent would take all of them down.

`--status` reads no config and writes no log, so a liveness probe can run it
every few seconds.  Keep `--max-age` several times `health_poll_interval_s`.

### `--once`

A smoke test of the whole configuration that leaves nothing running:

1. Starts all daemon scripts and lets them run for `--once-settle-s` seconds
   (default 15), then checks that each is still running.
2. Runs every scheduled script once and waits for all of them to finish
   (their `run_timeout_s` still applies).
3. Logs the health report as JSON and stops everything.

The exit status is **1** if a daemon had died by the check or a scheduled run
exited non-zero, so the command can gate a deployment or a container build.

```bash
bamboo-supervisor --config supervisor-agent.yaml --once
```

The settle time matters.  A daemon stopped within milliseconds of launch is
killed before it has installed its SIGTERM handler or opened its log file, so
it reports nothing; earlier versions of `--once` did exactly that.  If a
daemon is slow to start (heavy imports on a cold filesystem), raise
`--once-settle-s`.

With the full default configuration `--once` runs github-sync,
document-monitor and core-reaper back to back, which can take many minutes.

### Signals: stopping the supervisor

Ctrl-C, `kill -TERM <supervisor pid>`, `docker stop` and closing the terminal
or tmux pane (SIGHUP) all do the same thing: the supervisor sends SIGTERM to
every child, waits up to `stop_timeout_s`, and sends SIGKILL to any that are
left.

Each child runs in a session of its own, so terminal signals reach only the
supervisor.  (Before this, Ctrl-C reached the children directly and raced the
supervisor's SIGTERM, which produced spurious `killed by SIGTERM` warnings for
children that had already stopped cleanly.)

On Linux each child is also set to receive SIGTERM if the supervisor dies for
any reason, `kill -9` included, so no child outlives its supervisor.  That
safeguard is not available on macOS; there, killing the supervisor with
`kill -9` leaves its children running.

### Reading exit codes in the log

Child exits are reported in words.  `exited with code 0` is a clean exit.
`killed by SIGTERM` at shutdown, logged as a WARNING, means the child did
**not** shut down by itself: SIGTERM's default action killed it, usually
because it had not yet installed its handler.

---

## Log files

The supervisor writes to `supervisor-agent.log` (rotating, 10 MB × 5 backups).
Each managed script continues writing to its own log file as specified in its
`command` list.  Logs stay separate per script.

Key events logged by the supervisor:

- Start and stop of each script subprocess (with PID).
- Unexpected exits and restarts, including back-off delays.
- Scheduled one-shot dispatches and their exit codes.
- Dependency file waits.
- SIGTERM / SIGINT events and clean shutdown.

---

## Running as a system service

### systemd (Linux)

Create `/etc/systemd/system/bamboo-supervisor.service`:

```ini
[Unit]
Description=Bamboo MCP Services Supervisor
After=network.target

[Service]
Type=simple
User=bamboo
WorkingDirectory=/opt/bamboo-mcp-services
ExecStart=/opt/conda/envs/bamboo-mcp-services/bin/bamboo-supervisor \
    --config /opt/bamboo-mcp-services/supervisor-agent.yaml \
    --log-file /var/log/bamboo/supervisor-agent.log
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now bamboo-supervisor
sudo journalctl -u bamboo-supervisor -f
```

### launchd (macOS)

Create `~/Library/LaunchAgents/ch.cern.bamboo-supervisor.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>ch.cern.bamboo-supervisor</string>
  <key>ProgramArguments</key>
  <array>
    <string>/opt/conda/envs/bamboo-mcp-services/bin/bamboo-supervisor</string>
    <string>--config</string>
    <string>/Users/you/bamboo-mcp-services/supervisor-agent.yaml</string>
  </array>
  <key>WorkingDirectory</key>
  <string>/Users/you/bamboo-mcp-services</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/tmp/bamboo-supervisor.out</string>
  <key>StandardErrorPath</key>
  <string>/tmp/bamboo-supervisor.err</string>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/ch.cern.bamboo-supervisor.plist
```

---

## Health monitoring

### Current (log-based)

The supervisor logs a structured health summary after every tick in `--once`
mode.  For ongoing monitoring, `tail -f supervisor-agent.log` or
`journalctl -u bamboo-supervisor -f` shows restarts, back-off events, and
scheduled dispatch outcomes in real time.

### Future: HTTP health endpoint

A lightweight HTTP endpoint (`GET /health → JSON`) is planned for remote
deployments where operators need to query script status without SSH access.
The data structure is already defined — `bamboo-supervisor --once` logs
exactly what the endpoint would return.  The implementation will be a thin
wrapper around the existing `SupervisorAgent.health()` call and will be
added in a future release.

---

## Common issues

**`bamboo-supervisor: command not found`**

Run `pip install -e .` from the repository root to install the entry point.

**A script keeps restarting**

Check the script's own log file (e.g. `cric-agent.log`).  Back-off delays are
visible in `supervisor-agent.log` as `… fast failures in a row, back-off: next
attempt in …`.  If the script's own log file does not exist at all, the script
died before configuring logging: look in the supervisor's stdout/stderr, which
the children inherit.

**`Failed to launch daemon agent … No such file or directory`, repeating**

The command is not on the supervisor's `PATH`.  The supervisor starts children
by name, so start it from the activated environment, or put absolute paths to
the entry points in the `command` lists.

**`cric.duckdb` not found at startup**

Ensure `bamboo-cric` can reach the CVMFS path configured in `cric-agent.yaml`
(`cric_path`).  The ingestion script does not need `cric.duckdb`; if its own
`cric_path` is unreadable it falls back to its configured queue list.

**Scheduled script never runs**

Verify that the `command` list includes `--once`.  Without it the scheduled
process runs indefinitely; the supervisor never sees it finish, so it is
killed at `run_timeout_s` every time and logged as a timeout.
