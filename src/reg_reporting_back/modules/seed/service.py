"""Builds the demo system databases used by the POC.

Each system is a separate SQLite file under ``settings.data_dir``. Every file
carries its own ``lineage_metadata`` table, so tracing lineage means actually
opening the system the user connected and reading the metadata it publishes
about its own columns.

The values in the fixture reconcile end to end:

    exposure_value 1240.50 = off_bal_eur 1100.00 + allocated_value 140.50
    off_bal_eur    1100.00 = undrawn_eur  500.00 + drawn_eur      600.00
"""

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from ...core.config import settings
from ...core.database import reset_application_data
from ...core.exceptions import SeedError
from ...core.systems import SYSTEMS, SystemDefinition
from ..connections.repository import ConnectionRepository
from .schema import MESSAGE_RESET, MESSAGE_SEEDED, SeedSystemsResult, SystemInfo, to_system_info

IDENTIFIED = "IDENTIFIED"
REPORTING_DATE = "2026-06-30"

FACTS_COLUMNS = (
    "id",
    "row_code",
    "column_code",
    "attribute_name",
    "value",
    "currency",
    "reporting_date",
)

FACTS_DDL = """
CREATE TABLE {name} (
    id INTEGER PRIMARY KEY,
    row_code TEXT NOT NULL,
    column_code TEXT NOT NULL,
    attribute_name TEXT NOT NULL,
    value REAL NOT NULL,
    currency TEXT NOT NULL,
    reporting_date TEXT NOT NULL
)
"""

BALANCES_DDL = """
CREATE TABLE {name} (
    facility_id TEXT PRIMARY KEY,
    drawn_balance REAL NOT NULL,
    undrawn_balance REAL NOT NULL,
    currency TEXT NOT NULL
)
"""

BALANCE_COLUMNS = ("facility_id", "drawn_balance", "undrawn_balance", "currency")

COLLATERAL_DDL = """
CREATE TABLE {name} (
    collateral_id TEXT PRIMARY KEY,
    market_value REAL NOT NULL,
    currency TEXT NOT NULL
)
"""

COLLATERAL_COLUMNS = ("collateral_id", "market_value", "currency")

LINEAGE_METADATA_DDL = """
CREATE TABLE lineage_metadata (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    system_name      TEXT NOT NULL,
    table_name       TEXT NOT NULL,
    attribute_name   TEXT NOT NULL,
    report_code      TEXT,
    row_code         TEXT,
    column_code      TEXT,
    transformation   TEXT,
    source_refs_json TEXT,
    consumed_by      TEXT,
    status           TEXT NOT NULL DEFAULT 'IDENTIFIED'
)
"""

LINEAGE_METADATA_INDEXES = (
    "CREATE INDEX ix_metadata_lookup "
    "ON lineage_metadata (system_name, table_name, attribute_name)",
)

METADATA_COLUMNS = (
    "system_name",
    "table_name",
    "attribute_name",
    "report_code",
    "row_code",
    "column_code",
    "transformation",
    "source_refs_json",
    "consumed_by",
    "status",
)


@dataclass(frozen=True, slots=True)
class MetadataRow:
    """One published lineage relationship for a single attribute.

    ``sources`` is empty when the attribute is a system of record, which is how
    the explorer knows it has reached a final source.
    """

    table_name: str
    attribute_name: str
    sources: Sequence[str]
    transformation: str | None = None
    consumed_by: str | None = None
    report_code: str | None = None
    row_code: str | None = None
    column_code: str | None = None
    status: str = IDENTIFIED


@dataclass(frozen=True, slots=True)
class TableFixture:
    """A table to create, together with the rows to insert into it."""

    name: str
    ddl: str
    columns: tuple[str, ...]
    rows: tuple[tuple, ...] = ()


@dataclass(frozen=True, slots=True)
class SystemFixture:
    """Everything needed to build one system database."""

    definition: SystemDefinition
    tables: tuple[TableFixture, ...]
    metadata: tuple[MetadataRow, ...] = field(default_factory=tuple)


def _facts(name: str, rows: Sequence[tuple]) -> TableFixture:
    return TableFixture(name, FACTS_DDL.format(name=name), FACTS_COLUMNS, tuple(rows))


def _balances(name: str, rows: Sequence[tuple]) -> TableFixture:
    return TableFixture(name, BALANCES_DDL.format(name=name), BALANCE_COLUMNS, tuple(rows))


def _collateral(name: str, rows: Sequence[tuple]) -> TableFixture:
    return TableFixture(name, COLLATERAL_DDL.format(name=name), COLLATERAL_COLUMNS, tuple(rows))


FIXTURES: dict[str, SystemFixture] = {
    "reporting_db": SystemFixture(
        definition=SYSTEMS["reporting_db"],
        tables=(
            _facts(
                "c0700_facts",
                (
                    (1, "0010", "0200", "exposure_value", 1240.50, "EUR", REPORTING_DATE),
                    (2, "0040", "0200", "on_balance", 600.00, "EUR", REPORTING_DATE),
                    (3, "0050", "0200", "off_balance", 500.00, "EUR", REPORTING_DATE),
                ),
            ),
            # A second table that also owns a "value" column. It exists so the
            # metadata lookup has to use the datapoint context (report/row/column)
            # instead of matching on the attribute name alone.
            _facts(
                "c0800_facts",
                ((1, "0010", "0200", "collateral_value", 140.50, "EUR", REPORTING_DATE),),
            ),
        ),
        metadata=(
            MetadataRow(
                table_name="c0700_facts",
                attribute_name="value",
                sources=("sa_engine.calc_sa_exposure.exposure_value",),
                consumed_by="XBRL writer / COREP v1",
                report_code="C07.00",
                row_code="0010",
                column_code="0200",
            ),
            MetadataRow(
                table_name="c0700_facts",
                attribute_name="value",
                sources=("sa_engine.calc_sa_exposure.drawn_value",),
                consumed_by="XBRL writer / COREP v1",
                report_code="C07.00",
                row_code="0040",
                column_code="0200",
            ),
            MetadataRow(
                table_name="c0700_facts",
                attribute_name="value",
                sources=("sa_engine.calc_sa_exposure.undrawn_value",),
                consumed_by="XBRL writer / COREP v1",
                report_code="C07.00",
                row_code="0050",
                column_code="0200",
            ),
        ),
    ),
    "sa_engine": SystemFixture(
        definition=SYSTEMS["sa_engine"],
        tables=(
            TableFixture(
                "calc_sa_exposure",
                """
                CREATE TABLE calc_sa_exposure (
                    id INTEGER PRIMARY KEY,
                    exposure_value REAL NOT NULL,
                    drawn_value REAL NOT NULL,
                    undrawn_value REAL NOT NULL,
                    collateral_value REAL NOT NULL,
                    calculation_date TEXT NOT NULL
                )
                """,
                (
                    "id",
                    "exposure_value",
                    "drawn_value",
                    "undrawn_value",
                    "collateral_value",
                    "calculation_date",
                ),
                ((1, 1240.50, 600.00, 500.00, 140.50, REPORTING_DATE),),
            ),
        ),
        metadata=(
            MetadataRow(
                table_name="calc_sa_exposure",
                attribute_name="exposure_value",
                sources=(
                    "dwh.mart_sa_exposure.off_bal_eur",
                    "dwh.dw_collateral_alloc.allocated_value",
                ),
                transformation="off_bal_eur + allocated_value",
                consumed_by="reporting_db.c0700_facts.value",
            ),
            MetadataRow(
                table_name="calc_sa_exposure",
                attribute_name="drawn_value",
                sources=("dwh.mart_sa_exposure.drawn_eur",),
                transformation="SUM(drawn_eur)",
                consumed_by="reporting_db.c0700_facts.value",
            ),
            MetadataRow(
                table_name="calc_sa_exposure",
                attribute_name="undrawn_value",
                sources=("dwh.mart_sa_exposure.undrawn_eur",),
                transformation="SUM(undrawn_eur)",
                consumed_by="reporting_db.c0700_facts.value",
            ),
        ),
    ),
    "dwh": SystemFixture(
        definition=SYSTEMS["dwh"],
        tables=(
            TableFixture(
                "mart_sa_exposure",
                """
                CREATE TABLE mart_sa_exposure (
                    id INTEGER PRIMARY KEY,
                    off_bal_eur REAL NOT NULL,
                    drawn_eur REAL NOT NULL,
                    undrawn_eur REAL NOT NULL,
                    exposure_date TEXT NOT NULL
                )
                """,
                ("id", "off_bal_eur", "drawn_eur", "undrawn_eur", "exposure_date"),
                ((1, 1100.00, 600.00, 500.00, REPORTING_DATE),),
            ),
            TableFixture(
                "dw_exposure",
                """
                CREATE TABLE dw_exposure (
                    id INTEGER PRIMARY KEY,
                    drawn_eur REAL NOT NULL,
                    undrawn_eur REAL NOT NULL,
                    exposure_date TEXT NOT NULL
                )
                """,
                ("id", "drawn_eur", "undrawn_eur", "exposure_date"),
                ((1, 600.00, 500.00, REPORTING_DATE),),
            ),
            TableFixture(
                "dw_collateral_alloc",
                """
                CREATE TABLE dw_collateral_alloc (
                    id INTEGER PRIMARY KEY,
                    allocated_value REAL NOT NULL,
                    allocation_date TEXT NOT NULL
                )
                """,
                ("id", "allocated_value", "allocation_date"),
                ((1, 140.50, REPORTING_DATE),),
            ),
        ),
        metadata=(
            MetadataRow(
                table_name="mart_sa_exposure",
                attribute_name="off_bal_eur",
                sources=(
                    "dwh.dw_exposure.undrawn_eur",
                    "dwh.dw_exposure.drawn_eur",
                ),
                transformation="undrawn_eur + drawn_eur",
                consumed_by="sa_engine.calc_sa_exposure.exposure_value",
            ),
            MetadataRow(
                table_name="mart_sa_exposure",
                attribute_name="drawn_eur",
                sources=("dwh.dw_exposure.drawn_eur",),
                transformation="SUM(drawn_eur)",
                consumed_by="sa_engine.calc_sa_exposure.drawn_value",
            ),
            MetadataRow(
                table_name="mart_sa_exposure",
                attribute_name="undrawn_eur",
                sources=("dwh.dw_exposure.undrawn_eur",),
                transformation="SUM(undrawn_eur)",
                consumed_by="sa_engine.calc_sa_exposure.undrawn_value",
            ),
            MetadataRow(
                table_name="dw_exposure",
                attribute_name="drawn_eur",
                sources=("staging.stg_facilities.drawn_balance",),
                consumed_by="dwh.mart_sa_exposure.drawn_eur",
            ),
            MetadataRow(
                table_name="dw_exposure",
                attribute_name="undrawn_eur",
                sources=("staging.stg_facilities.undrawn_balance",),
                consumed_by="dwh.mart_sa_exposure.undrawn_eur",
            ),
            MetadataRow(
                table_name="dw_collateral_alloc",
                attribute_name="allocated_value",
                sources=("staging.stg_collateral.market_value",),
                consumed_by="sa_engine.calc_sa_exposure.exposure_value",
            ),
        ),
    ),
    "staging": SystemFixture(
        definition=SYSTEMS["staging"],
        tables=(
            _balances("stg_facilities", (("FAC-001", 600.00, 500.00, "EUR"),)),
            _collateral("stg_collateral", (("COL-001", 140.50, "EUR"),)),
        ),
        metadata=(
            MetadataRow(
                table_name="stg_facilities",
                attribute_name="drawn_balance",
                sources=("loans_db.facility.drawn_balance",),
                consumed_by="dwh.dw_exposure.drawn_eur",
            ),
            MetadataRow(
                table_name="stg_facilities",
                attribute_name="undrawn_balance",
                sources=("loans_db.facility.undrawn_balance",),
                consumed_by="dwh.dw_exposure.undrawn_eur",
            ),
            MetadataRow(
                table_name="stg_collateral",
                attribute_name="market_value",
                sources=("collateral_db.collateral.market_value",),
                consumed_by="dwh.dw_collateral_alloc.allocated_value",
            ),
        ),
    ),
    "loans_db": SystemFixture(
        definition=SYSTEMS["loans_db"],
        tables=(
            _balances("facility", (("FAC-001", 600.00, 500.00, "EUR"),)),
        ),
        metadata=(
            MetadataRow(
                table_name="facility",
                attribute_name="drawn_balance",
                sources=(),
                consumed_by="staging.stg_facilities.drawn_balance",
            ),
            MetadataRow(
                table_name="facility",
                attribute_name="undrawn_balance",
                sources=(),
                consumed_by="staging.stg_facilities.undrawn_balance",
            ),
        ),
    ),
    "collateral_db": SystemFixture(
        definition=SYSTEMS["collateral_db"],
        tables=(
            _collateral("collateral", (("COL-001", 140.50, "EUR"),)),
        ),
        metadata=(
            MetadataRow(
                table_name="collateral",
                attribute_name="market_value",
                sources=(),
                consumed_by="staging.stg_collateral.market_value",
            ),
        ),
    ),
}


def system_database_path(definition: SystemDefinition) -> Path:
    """Return the on-disk path of a system database."""

    return settings.data_dir / definition.database_file


@contextmanager
def _writable_connection(path: Path) -> Iterator[sqlite3.Connection]:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        yield conn
    except sqlite3.Error as exc:
        raise SeedError(f"Could not build '{path.name}': {exc}") from exc
    finally:
        conn.close()


def _insert_rows(conn: sqlite3.Connection, table: TableFixture) -> None:
    if not table.rows:
        return
    columns = ", ".join(table.columns)
    placeholders = ", ".join("?" * len(table.columns))
    conn.executemany(
        f"INSERT INTO {table.name} ({columns}) VALUES ({placeholders})",
        table.rows,
    )


def _insert_metadata(
    conn: sqlite3.Connection,
    definition: SystemDefinition,
    rows: Sequence[MetadataRow],
) -> None:
    columns = ", ".join(METADATA_COLUMNS)
    placeholders = ", ".join("?" * len(METADATA_COLUMNS))
    conn.executemany(
        f"INSERT INTO lineage_metadata ({columns}) VALUES ({placeholders})",
        [
            (
                definition.name,
                row.table_name,
                row.attribute_name,
                row.report_code,
                row.row_code,
                row.column_code,
                row.transformation,
                json.dumps(list(row.sources)),
                row.consumed_by,
                row.status,
            )
            for row in rows
        ],
    )


def build_system(fixture: SystemFixture) -> Path:
    """(Re)create a single system database from its fixture."""

    definition = fixture.definition
    path = system_database_path(definition)

    if path.exists():
        path.unlink()

    with _writable_connection(path) as conn:
        conn.execute(LINEAGE_METADATA_DDL)
        for statement in LINEAGE_METADATA_INDEXES:
            conn.execute(statement)

        for table in fixture.tables:
            conn.execute(table.ddl)
            _insert_rows(conn, table)

        _insert_metadata(conn, definition, fixture.metadata)
        conn.commit()

    return path


def seed_all() -> list[Path]:
    """(Re)create every demo system database."""

    return [build_system(fixture) for fixture in FIXTURES.values()]


def seeded_systems() -> list[str]:
    """Names of the systems whose database file currently exists."""

    return [
        definition.name
        for definition in SYSTEMS.values()
        if system_database_path(definition).exists()
    ]


def _connected_systems() -> set[str]:
    return {item.system_name for item in ConnectionRepository.list_all() if item.connected}


def list_systems() -> list[SystemInfo]:
    """Return the system registry annotated with seeded/connected state."""

    seeded = set(seeded_systems())
    connected = _connected_systems()

    return [
        to_system_info(definition, seeded=definition.name in seeded, connected=definition.name in connected)
        for definition in SYSTEMS.values()
    ]


def build_databases() -> SeedSystemsResult:
    """Create every demo system database, keeping existing connections."""

    seed_all()

    return SeedSystemsResult(
        message=MESSAGE_SEEDED,
        systems=list_systems(),
        data_dir=str(settings.data_dir),
    )


def reset() -> SeedSystemsResult:
    """Rebuild the demo databases and forget reports, traces and connections."""

    seed_all()
    reset_application_data()

    return SeedSystemsResult(
        message=MESSAGE_RESET,
        systems=list_systems(),
        data_dir=str(settings.data_dir),
    )