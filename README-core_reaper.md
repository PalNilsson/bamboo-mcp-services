# Core-dump workspace reaper (`core-reaper`)

Reports reclaimable PanDA core-dump analysis workspaces.

> ⚠️ **This version removes nothing.**
> It logs `I could have removed: <path>` for every workspace that passes the
> safety rules and the path guard, and exits. There is no `--apply` flag, and
> the module contains no deletion code at all — a unit test enforces that by
> parsing its own source. Deletion will only be enabled once these reports have
> been reviewed against real production data.

---

## Why it exists

`atlas.core_dump_analysis` in **Bamboo MCP** downloads PanDA job core dumps —
routinely **1 GB each** — to local disk and never deletes anything. That is a
deliberate rule in that codebase, stated in its module docstring:

> Nothing here deletes anything — not partial downloads, not failed workspaces,
> not superseded evidence. Reaping is a separate concern and deliberately
> absent.

This script is that separate concern. Bamboo MCP keeps its no-delete rule.

Today the only thing between an unattended deployment and a full `/tmp` is a
byte quota that *refuses new work* rather than freeing space: when it trips,
core-dump analysis simply stops working until someone clears the directory by
hand. The reaper's job is to make that moment visible before it arrives — and,
later, to keep the promise in the quota message.

---

## Quick start

```bash
# One sweep, human-readable summary:
bamboo-core-reaper --once

# Machine-readable, for monitoring:
bamboo-core-reaper --once --format json

# Sweep a non-default root (see "Path guard" — a root outside /tmp/bamboo
# reports usage but refuses every candidate):
bamboo-core-reaper --once --root /tmp/bamboo/core-analysis

# See every skip decision and its reason:
bamboo-core-reaper --once --log-level DEBUG
```

Under the supervisor it runs as a scheduled one-shot every hour; the entry is
already in `supervisor-agent.yaml`.

Sample output:

```
Root:         /tmp/bamboo/core-analysis
Usage:        43.9 GiB (87.8% of 50.0 GiB)
Workspaces:   38
Reclaimable:  31.2 GiB across 26 candidate(s)
  could have removed  /tmp/bamboo/core-analysis/job-7272161793/job  [prune 1.0 GiB terminal-and-aged:complete]
  could have removed  /tmp/bamboo/core-analysis/job-7272161794/job  [prune 1.0 GiB terminal-and-aged:failed]
  could have removed  /tmp/bamboo/core-analysis/job-7272161799       [purge 1.0 GiB orphan-no-manifest]
Nothing was removed — this build has no deletion code.
```

---

## What it would remove

Two modes. **Prune is the default**, and it is the one worth defaulting to:
`job/` holds essentially all of the bytes, while the manifest, `evidence.json`,
`gdb_raw.txt` and `worker.log` together are kilobytes and are the entire
diagnostic record of a run. Pruning reclaims ~100% of the space and loses
nothing a human would want later.

| Mode | Target | Survives |
|---|---|---|
| `prune` (default) | `<workspace>/job/` | manifest, `evidence.json`, `gdb_raw.txt`, `worker.log`, and any incidental clutter such as a stray `probe.sh` |
| `purge` | the whole `job-<id>/` directory | nothing |

`--purge-failed-after-hours N` escalates old `failed` runs to whole-workspace
removal while everything else stays on prune.

---

## Safety rules

A workspace is reported as reclaimable only when **all four** hold:

1. `state` in the manifest is `complete` or `failed`.
2. `finished_utc` (falling back to `updated_utc`) is older than `--min-age-hours`.
3. It does not currently hold the `.busy.lock` slot.
4. `worker_pid` is `null` or not a live process.

Rules 1 and 4 are checked **independently**. A manifest can be stale — a worker
that died without recording anything leaves a non-terminal state and a dead
PID — and Bamboo MCP has its own `reconcile_state` for that case. The reaper
runs out-of-process and must not assume reconciliation has happened, so it
leaves those workspaces alone.

**Workers are never signalled.** Liveness is checked by looking for
`/proc/<pid>`; no signal is sent, not even signal 0. PID reuse can only make
the check report a dead worker as alive, which makes the reaper more
conservative, never less.

### Workspaces with no manifest

Ambiguous — possibly a run that crashed between `mkdir` and the first manifest
write. They get a much longer threshold (`--orphan-age-hours`, default 24 h)
and a distinct reason code, `orphan-no-manifest`, so they are easy to grep for.

### The lock

`.busy.lock` holds two separate things and conflating them causes bugs:

- the **`fcntl.flock`** guards only the read-modify-write of the file and is
  held for microseconds;
- the **content** is the ownership record — a JSON object naming the
  `request_id` and `job_id` holding the slot, `{}` when free — and it outlives
  both the flock and the process that wrote it.

Holding the flock is therefore not the same as holding the slot, and a free
flock does not mean no analysis is running. The reaper takes the flock only for
the duration of the read, then decides from the content. The file is opened
read-only; it is never created, rewritten or removed. Clearing a stale slot is
deliberately **not** implemented here — that would be a write.

---

## Path guard

Every candidate passes through `assert_reclaimable_path()` before it is even
reported. Nothing deletes yet, so this exists to be exercised against real
production layouts now, and to give a future `--apply` exactly one function to
call immediately before each unlink.

Seven independent checks:

| Check | Rejection reason |
|---|---|
| Under the hardcoded prefix allowlist (`/tmp/bamboo`), both literally and after resolution | `outside-allowlist` |
| Strictly under the configured root | `not-under-root` / `escapes-root` |
| No symlink in any component below the root | `symlink-in-path` |
| Not `/`, `/tmp`, `/tmp/bamboo`, `/data`, `/data/bamboo`, the root itself, or anything implausibly shallow | `forbidden-target` / `too-shallow` |
| Correct shape: `job-<digits>` for a workspace, `<job-N>/job` for a prune | `not-a-workspace-name` / `not-a-job-dir` |
| Same filesystem as the root | `cross-filesystem` |
| Not `.busy.lock`, the manifest, or a preserved diagnostic file | `reserved-name` |

**The allowlist is hardcoded** in `agent.py` and there is no flag or
environment variable to widen it. A deployment that moves
`BAMBOO_CORE_ANALYSIS_ROOT` outside `/tmp/bamboo` — say to `/data/bamboo/tmp` —
still gets a usage report, but every candidate is refused and logged:

```
ERROR  Analysis root /data/bamboo/tmp is outside the hardcoded allowlist ['/tmp/bamboo'] —
       every candidate will be refused. Widening the allowlist is a code change in agent.py.
```

That is intended. Widening it should cost a code review.

---

## Quota pressure

Two independent triggers, either sufficient:

- **Age** — works standalone, so a quiet deployment does not accumulate
  month-old gigabytes just because it never hit the ceiling.
- **Pressure** — when usage reaches `--pressure-pct` of the quota (default 80%
  of `BAMBOO_CORE_ANALYSIS_QUOTA_BYTES`, itself defaulting to 50 GiB), a second
  pass walks the age-blocked workspaces oldest-first until projected usage
  falls below `--target-pct`.

Pressure relaxes **the age rule and nothing else**. Terminal state, slot
ownership and worker liveness are re-checked identically, and
`--min-age-floor-hours` (default 0.5) means an analysis that finished moments
ago is never reached.

Usage is measured the way Bamboo MCP's `workspace_usage_bytes()` does it:
recursive walk, symlinks skipped, unreadable entries ignored so one bad file
cannot abort a sweep, summing `st_size`. `st_size` rather than `st_blocks`
keeps the arithmetic identical to `check_quota()`'s, at the cost of overstating
sparse core files.

---

## Options

| Flag | Default | Meaning |
|---|---|---|
| `--root PATH` | `$BAMBOO_CORE_ANALYSIS_ROOT`, then `/tmp/bamboo/core-analysis` | Directory to sweep |
| `--min-age-hours H` | 1.0 | Retention age for terminal workspaces |
| `--orphan-age-hours H` | 24.0 | Longer threshold for workspaces with no manifest |
| `--mode {prune,purge}` | `prune` | What would be removed |
| `--purge-failed-after-hours H` | off | Escalate old `failed` runs to whole-workspace removal |
| `--quota-bytes N` | `$BAMBOO_CORE_ANALYSIS_QUOTA_BYTES`, then 50 GiB | Ceiling for pressure maths |
| `--pressure-pct PCT` | 80 | Usage at which the pressure pass engages |
| `--target-pct PCT` | 60 | Usage the pressure pass aims to get below |
| `--min-age-floor-hours H` | 0.5 | Floor the pressure pass may never cross |
| `--format {text,json}` | `text` | Summary format on stdout |
| `--config PATH` | bundled YAML | Config file; a missing file is not an error |
| `--once` | off | One sweep then exit |
| `--log-level`, `--log-file` | INFO, `core-reaper-agent.log` | Standard logging flags |

CLI flags beat the config file, which beats the defaults.

The retention age is deliberately short. Several people analysing core dumps on
a shared node fill a disk quickly, and since nothing is deleted the setting
only controls how much you get to see.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Sweep completed; usage below the pressure threshold |
| 1 | Sweep completed with non-fatal errors, or an unhandled exception |
| 2 | Usage error (bad arguments or unreadable config) |
| 3 | Usage at or above the pressure threshold **and** reclaimable space found — someone needs to free space by hand |

Exit 3 is the actionable one while the reaper is report-only: wire it to an
alert.

---

## Reason codes

Every skipped workspace is logged at DEBUG with one of these, and the counts
appear in the summary and the JSON report.

| Reason | Meaning |
|---|---|
| `terminal-and-aged:<state>` | Reportable (this is a candidate, not a skip) |
| `orphan-no-manifest` | No manifest, idle longer than the orphan threshold |
| `holds-slot` | Currently owns `.busy.lock` |
| `non-terminal-state:<state>` | Run may still be in progress |
| `worker-alive` | `worker_pid` is a live process |
| `too-young` | Only the age rule failed; eligible for the pressure pass |
| `orphan-too-young` | No manifest, but not idle long enough |
| `no-timestamp` | Manifest has neither `finished_utc` nor `updated_utc` |
| `already-pruned` | `job/` is already gone |
| `job-dir-symlink` | `job/` is a symlink — never followed, never targeted |
| `manifest-unparseable` | Manifest is not readable JSON |
| `manifest-version-unsupported` | Unknown `manifest_version`; refused rather than guessed at |

---

## Files it knows about

```
$BAMBOO_CORE_ANALYSIS_ROOT/
    .busy.lock                      read-only; never created, rewritten or removed
    job-<job_id>/                   one workspace per PanDA job
        .bamboo-core-analysis.json  manifest — read-only, never written
        job/                        the bytes: core.<pid>, core.<pid>.part, workDir/ …
        evidence.json               preserved by prune
        gdb_raw.txt                 preserved by prune
        worker.log                  preserved by prune
```

Anything at the root that is not a `job-<digits>` directory is counted toward
usage, reported as `unmanaged`, and never targeted.

---

## Development

```bash
pytest tests/agents/core_reaper_agent/ -v     # 116 tests
flake8 src/bamboo_mcp_services/agents/core_reaper_agent
```

`TestNoDeletionInvariant` parses `agent.py`'s AST and fails if the module ever
calls a removing or truncating function, imports `shutil` or `subprocess`,
opens a file for writing, or calls `os.open` with anything but `O_RDONLY`. It
also asserts a full sweep leaves the tree byte-for-byte identical. Do not
delete those tests casually — when deletion is eventually enabled, they are the
deliberate gate to walk through.
