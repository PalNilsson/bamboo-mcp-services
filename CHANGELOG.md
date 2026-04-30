# Changelog

All notable changes to this project will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

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
