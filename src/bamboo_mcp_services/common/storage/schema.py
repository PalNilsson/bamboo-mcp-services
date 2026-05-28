"""Database schema definitions for BigPanda jobs data.

This module is the single source of truth for the DuckDB table layout used by the
ingestion-agent to persist BigPanda job data.  It can be imported by AskPanDA (or any
other consumer) to validate, recreate, or introspect the schema without having to
hard-code SQL strings in multiple places.

DuckDB does expose full introspection via ``DESCRIBE <table>`` and
``information_schema.columns``, so a consuming application can always discover the
schema at runtime.  This module still exists for three complementary reasons:

1. **Documentation** – a single human-readable definition of every column.
2. **Reproducibility** – tests and CI can recreate an empty DB with ``apply_schema()``.
3. **Portability** – AskPanDA can import and reuse exactly the same DDL without
   duplicating SQL strings.

Tables
------
``jobs``
    One row per PanDA job fetched from ``/jobs/?computingsite=<QUEUE>&json&hours=1``.
    Primary key is ``pandaid``.  The row is upserted on each ingestion cycle so the
    table always reflects the latest known state of each job.

    Additional bookkeeping columns (prefixed ``_``) are added by the ingestion agent:

    * ``_queue``        – the BigPanda queue name that was queried
    * ``_fetched_utc``  – UTC timestamp of the ingestion run

``selectionsummary``
    Stores the ``selectionsummary`` list from the same API response.  Each element
    describes aggregate statistics for a facet field (e.g. ``jobstatus``).  The rows
    are linked to a ``_queue`` / ``_fetched_utc`` pair so they can be correlated with
    the jobs snapshot.  Stored as JSON because the ``list`` sub-structure is variable.

``errors_by_count``
    Stores the ``errsByCount`` list – a ranked list of error codes with counts and an
    example pandaid.
"""
from __future__ import annotations

import duckdb

# ---------------------------------------------------------------------------
# DDL strings
# ---------------------------------------------------------------------------

#: Typed ``jobs`` table.  All columns from jobs1h_schema.json are represented;
#: columns whose schema type was ``null`` (always-null in the sample) are stored
#: as VARCHAR so they can hold a value if the API ever returns one.
JOBS_DDL = """
CREATE TABLE IF NOT EXISTS jobs (
    -- Primary key
    pandaid                  BIGINT PRIMARY KEY,

    -- Identity / scheduling
    jobdefinitionid          BIGINT,
    schedulerid              VARCHAR,
    pilotid                  VARCHAR,
    taskid                   BIGINT,
    jeditaskid               BIGINT,
    reqid                    BIGINT,
    jobsetid                 BIGINT,
    workqueue_id             INTEGER,

    -- Timestamps
    creationtime             TIMESTAMP,
    modificationtime         TIMESTAMP,
    statechangetime          TIMESTAMP,
    proddbupdatetime         TIMESTAMP,
    starttime                TIMESTAMP,
    endtime                  TIMESTAMP,

    -- Hosts
    creationhost             VARCHAR,
    modificationhost         VARCHAR,
    computingsite            VARCHAR,
    computingelement         VARCHAR,
    nucleus                  VARCHAR,
    wn                       VARCHAR,

    -- Software / release
    atlasrelease             VARCHAR,
    transformation           VARCHAR,
    homepackage              VARCHAR,
    cmtconfig                VARCHAR,
    container_name           VARCHAR,
    cpu_architecture_level   VARCHAR,

    -- Labels / classification
    prodserieslabel          VARCHAR,
    prodsourcelabel          VARCHAR,
    produserid               VARCHAR,
    gshare                   VARCHAR,
    grid                     VARCHAR,
    cloud                    VARCHAR,
    homecloud                VARCHAR,
    transfertype             VARCHAR,
    resourcetype             VARCHAR,
    eventservice             VARCHAR,
    job_label                VARCHAR,
    category                 VARCHAR,
    lockedby                 VARCHAR,
    relocationflag           INTEGER,
    jobname                  VARCHAR,
    ipconnectivity           VARCHAR,
    processor_type           VARCHAR,

    -- Priority
    assignedpriority         INTEGER,
    currentpriority          INTEGER,
    priorityrange            VARCHAR,
    jobsetrange              VARCHAR,

    -- Attempts
    attemptnr                INTEGER,
    maxattempt               INTEGER,
    failedattempt            INTEGER,

    -- Status
    jobstatus                VARCHAR,
    jobsubstatus             VARCHAR,
    commandtopilot           VARCHAR,
    transexitcode            VARCHAR,

    -- Error codes & diagnostics
    piloterrorcode           INTEGER,
    piloterrordiag           VARCHAR,
    exeerrorcode             INTEGER,
    exeerrordiag             VARCHAR,
    superrorcode             INTEGER,
    superrordiag             VARCHAR,
    ddmerrorcode             INTEGER,
    ddmerrordiag             VARCHAR,
    brokerageerrorcode       INTEGER,
    brokerageerrordiag       VARCHAR,
    jobdispatchererrorcode   INTEGER,
    jobdispatchererrordiag   VARCHAR,
    taskbuffererrorcode      INTEGER,
    taskbuffererrordiag      VARCHAR,
    errorinfo                VARCHAR,
    error_desc               VARCHAR,
    transformerrordiag       VARCHAR,

    -- Data blocks
    proddblock               VARCHAR,
    dispatchdblock           VARCHAR,
    destinationdblock        VARCHAR,
    destinationse            VARCHAR,
    sourcesite               VARCHAR,
    destinationsite          VARCHAR,

    -- Resource limits
    maxcpucount              INTEGER,
    maxcpuunit               VARCHAR,
    maxdiskcount             INTEGER,
    maxdiskunit              VARCHAR,
    minramcount              INTEGER,
    minramunit               VARCHAR,
    corecount                INTEGER,
    actualcorecount          INTEGER,
    meancorecount            VARCHAR,   -- always null in sample
    maxwalltime              INTEGER,

    -- CPU / HS06
    cpuconsumptiontime       DOUBLE,
    cpuconsumptionunit       VARCHAR,
    cpuconversion            VARCHAR,   -- always null in sample
    cpuefficiency            DOUBLE,
    hs06                     INTEGER,
    hs06sec                  VARCHAR,   -- always null in sample

    -- Memory metrics (kB)
    maxrss                   BIGINT,
    maxvmem                  BIGINT,
    maxswap                  BIGINT,
    maxpss                   BIGINT,
    avgrss                   BIGINT,
    avgvmem                  BIGINT,
    avgswap                  BIGINT,
    avgpss                   BIGINT,
    maxpssgbpercore          DOUBLE,

    -- I/O metrics
    totrchar                 VARCHAR,   -- always null in sample
    totwchar                 VARCHAR,   -- always null in sample
    totrbytes                VARCHAR,   -- always null in sample
    totwbytes                VARCHAR,   -- always null in sample
    raterchar                VARCHAR,   -- always null in sample
    ratewchar                VARCHAR,   -- always null in sample
    raterbytes               VARCHAR,   -- always null in sample
    ratewbytes               VARCHAR,   -- always null in sample
    diskio                   BIGINT,
    memoryleak               VARCHAR,   -- always null in sample
    memoryleakx2             VARCHAR,   -- always null in sample

    -- Events / files
    nevents                  INTEGER,
    ninputdatafiles          INTEGER,
    inputfiletype            VARCHAR,
    inputfileproject         VARCHAR,
    inputfilebytes           BIGINT,
    noutputdatafiles         INTEGER,
    outputfilebytes          BIGINT,
    outputfiletype           VARCHAR,

    -- Duration helpers (computed by BigPanda, stored for convenience)
    durationsec              DOUBLE,
    durationmin              INTEGER,
    duration                 VARCHAR,
    waittimesec              DOUBLE,
    waittime                 VARCHAR,

    -- Pilot
    pilotversion             VARCHAR,

    -- Carbon
    gco2_regional            VARCHAR,   -- always null in sample
    gco2_global              VARCHAR,   -- always null in sample

    -- Misc
    jobmetrics               VARCHAR,
    jobinfo                  VARCHAR,
    consumer                 VARCHAR,   -- always null in sample

    -- Bookkeeping added by ingestion agent
    _queue                   VARCHAR    NOT NULL,
    _fetched_utc             TIMESTAMP  NOT NULL
);
"""

#: Summary facets from the ``selectionsummary`` key.  The ``list`` and ``stats``
#: sub-structures vary per field so they are stored as JSON to avoid an overly
#: complex normalised schema.
SELECTION_SUMMARY_DDL = """
CREATE TABLE IF NOT EXISTS selectionsummary (
    id           INTEGER  NOT NULL,        -- position within this queue's response
    field        VARCHAR  NOT NULL,
    list_json    JSON     NOT NULL,        -- JSON array of {kname, kvalue} objects
    stats_json   JSON,                    -- JSON object with aggregate stats
    _queue       VARCHAR  NOT NULL,
    _fetched_utc TIMESTAMP NOT NULL,
    PRIMARY KEY (id, _queue)              -- id is only unique within a queue
);
"""

#: Error-code frequency table from the ``errsByCount`` key.
ERRORS_BY_COUNT_DDL = """
CREATE TABLE IF NOT EXISTS errors_by_count (
    id              INTEGER  NOT NULL,    -- rank within this queue's response
    error           VARCHAR,
    codename        VARCHAR,
    codeval         INTEGER,
    diag            VARCHAR,
    error_desc_text VARCHAR,
    example_pandaid BIGINT,
    count           INTEGER,
    pandalist_json  JSON,                 -- raw pandalist, structure varies
    _queue          VARCHAR  NOT NULL,
    _fetched_utc    TIMESTAMP NOT NULL,
    PRIMARY KEY (id, _queue)             -- id is only unique within a queue
);
"""

#: Ordered list of all DDL statements that make up the full schema.
ALL_DDL: list[str] = [
    JOBS_DDL,
    SELECTION_SUMMARY_DDL,
    ERRORS_BY_COUNT_DDL,
]


def apply_schema(conn: duckdb.DuckDBPyConnection) -> None:
    """Create all schema tables in *conn* if they do not already exist.

    This is idempotent – safe to call on an existing database.

    If a pre-existing database has the old single-column ``PRIMARY KEY`` on
    ``selectionsummary.id`` or ``errors_by_count.id`` (which breaks when more
    than one queue is ingested), the affected tables are dropped and recreated
    with the correct composite ``PRIMARY KEY (id, _queue)``.  Data in those
    tables is lost, but both are replaced on every ingestion cycle anyway.

    Args:
        conn: An open DuckDB connection.
    """
    _migrate_composite_pk(conn)
    for ddl in ALL_DDL:
        conn.execute(ddl)


def _migrate_composite_pk(conn: duckdb.DuckDBPyConnection) -> None:
    """Drop tables whose primary key is the old single-column ``id``.

    This is a one-time migration for databases created before the composite
    ``PRIMARY KEY (id, _queue)`` was introduced.  The tables contain only
    per-cycle snapshots that are fully replaced on the next ingestion run, so
    dropping them is safe.

    Each drop is wrapped in an explicit transaction so that a concurrent reader
    never observes the table in a transient absent state (DuckDB MVCC ensures
    the reader keeps its snapshot until commit, but wrapping the drop makes the
    intent explicit and guards against future engine changes).

    Args:
        conn: An open DuckDB connection.
    """
    for table in ("selectionsummary", "errors_by_count"):
        try:
            constraints = conn.execute(
                "SELECT constraint_type, constraint_text "
                "FROM duckdb_constraints() "
                "WHERE table_name = ?",
                [table],
            ).fetchall()
        except Exception:
            continue  # table doesn't exist yet — nothing to migrate

        for ctype, ctext in constraints:
            # Old schema: PRIMARY KEY on id alone ("PRIMARY KEY(id)")
            # New schema: PRIMARY KEY on (id, _queue)
            if ctype == "PRIMARY KEY" and "_queue" not in (ctext or ""):
                conn.execute("BEGIN")
                try:
                    conn.execute(f"DROP TABLE IF EXISTS {table}")
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
                break


def table_names() -> list[str]:
    """Return the names of all tables defined in this schema.

    Returns:
        List of table name strings.
    """
    return ["jobs", "selectionsummary", "errors_by_count"]
