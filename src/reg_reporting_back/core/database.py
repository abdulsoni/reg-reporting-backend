"""Access to this service's own SQLite database.

The application database holds uploaded reports, their datapoints, lineage
traces and the registry of connected systems. It is never used to reach a
client system: that always goes through a connection adapter.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Connection, Engine, create_engine, event, text
from sqlalchemy.pool import StaticPool

from .config import settings

APP_SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    id                  TEXT PRIMARY KEY,
    file_name           TEXT NOT NULL,
    report_type         TEXT,
    reporting_entity    TEXT,
    reporting_date      TEXT,
    submission_version  TEXT,
    page_count          INTEGER NOT NULL DEFAULT 0,
    table_count         INTEGER NOT NULL DEFAULT 0,
    raw_json            TEXT,
    pdf_bytes           BLOB,
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS report_datapoints (
    id              TEXT PRIMARY KEY,
    report_id       TEXT NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    report_code     TEXT,
    report_title    TEXT,
    row_code        TEXT,
    column_code     TEXT,
    attribute_name  TEXT,
    value           REAL,
    unit            TEXT,
    currency        TEXT
);

CREATE TABLE IF NOT EXISTS lineage_traces (
    id            TEXT PRIMARY KEY,
    report_id     TEXT,
    datapoint_id  TEXT,
    origin_system TEXT,
    status        TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lineage_hops (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id         TEXT NOT NULL REFERENCES lineage_traces(id) ON DELETE CASCADE,
    hop_number       INTEGER NOT NULL,
    branch_path      TEXT NOT NULL,
    system_name      TEXT NOT NULL,
    table_name       TEXT NOT NULL,
    attribute_name   TEXT NOT NULL,
    transformation   TEXT,
    source_from      TEXT,
    source_refs_json TEXT,
    consumed_by      TEXT,
    status           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS system_connections (
    system_name   TEXT PRIMARY KEY,
    db_type       TEXT NOT NULL,
    database_name TEXT NOT NULL,
    connected     INTEGER NOT NULL DEFAULT 0,
    connected_at  TEXT,
    params_json   TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_hops_path
    ON lineage_hops (trace_id, branch_path);

CREATE INDEX IF NOT EXISTS ix_hops_trace
    ON lineage_hops (trace_id, hop_number);

CREATE INDEX IF NOT EXISTS ix_datapoints_report
    ON report_datapoints (report_id);
"""

# The sqlite3 driver only allows one statement per execute() call.
APP_SCHEMA_STATEMENTS = tuple(
    statement.strip() for statement in APP_SCHEMA.split(";") if statement.strip()
)

# Columns added after a database was first created. `CREATE TABLE IF NOT EXISTS`
# leaves an existing table untouched, so these are applied separately and are
# safe to run against an up-to-date database.
SCHEMA_MIGRATIONS = (
    ("lineage_traces", "origin_system", "TEXT"),
    ("system_connections", "params_json", "TEXT"),
)


_ENGINES: dict[str, Engine] = {}


@lru_cache(maxsize=4)
def _build_engine(url: str) -> Engine:
    """Build (and memoise) one engine per database URL."""

    if not url.startswith("sqlite"):
        engine = create_engine(url, pool_pre_ping=True)
    else:
        engine = create_engine(
            url,
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )

        @event.listens_for(engine, "connect")
        def _enable_foreign_keys(dbapi_connection, _record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys = ON")
            cursor.close()

    _ENGINES[url] = engine
    return engine


def get_engine() -> Engine:
    """Return the engine for the configured application database."""

    url = settings.app_database_url
    if url.startswith("sqlite") and ":memory:" not in url:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
    return _build_engine(url)


def reset_engines() -> None:
    """Dispose every cached engine. Used by tests and the seed endpoints."""

    for engine in _ENGINES.values():
        engine.dispose()
    _ENGINES.clear()
    _build_engine.cache_clear()


@contextmanager
def session() -> Iterator[Connection]:
    """Yield a connection inside a transaction, committing on success."""

    with get_engine().begin() as conn:
        yield conn


def _existing_tables(conn: Connection) -> set[str]:
    rows = conn.execute(
        text("SELECT name FROM sqlite_master WHERE type = 'table'")
    ).mappings()
    return {row["name"] for row in rows}


def _existing_columns(conn: Connection, table: str) -> set[str]:
    rows = conn.execute(text(f"PRAGMA table_info({table})")).mappings()
    return {row["name"] for row in rows}


def _apply_migrations(conn: Connection) -> None:
    for table, column, column_type in SCHEMA_MIGRATIONS:
        if table in _existing_tables(conn) and column not in _existing_columns(conn, table):
            conn.execute(
                text(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")
            )


def create_schema() -> None:
    """Create the application database tables if they do not exist yet."""

    with session() as conn:
        for statement in APP_SCHEMA_STATEMENTS:
            conn.execute(text(statement))
        _apply_migrations(conn)


RESET_STATEMENTS = (
    "DELETE FROM lineage_hops",
    "DELETE FROM lineage_traces",
    "DELETE FROM report_datapoints",
    "DELETE FROM reports",
    "DELETE FROM system_connections",
)


def reset_application_data() -> None:
    """Delete every report, trace and connection from the application database."""

    create_schema()
    with session() as conn:
        for statement in RESET_STATEMENTS:
            conn.execute(text(statement))