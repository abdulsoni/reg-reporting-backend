"""Builds the demo system databases used by the POC.

Each system is a separate SQLite file under ``settings.data_dir``. Every file
carries its own ``lineage_metadata`` table, so tracing lineage means actually
opening the system the user connected and reading the metadata it publishes
about its own columns.

The values in the fixture reconcile end to end:

    exposure_value 1240.50 = off_bal_eur 1100.00 + allocated_value 140.50
    off_bal_eur    1100.00 = undrawn_eur  500.00 + drawn_eur      600.00

Scenario B (normalized sensitivity) mostly reconciles:

    adjusted_local 460.23 = original_local 500.25 + approved_adjustment_local -40.02
    normalized_usd   4.60 = adjusted_local 460.23 * fx_rate 0.01

NORM-SENS-00005 is the deliberate exception: the sheet publishes 3.18 while
the formula produces 4.6023, so the trace can demonstrate a flagged mismatch.
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

NORMALIZED_SENSITIVITY_DDL = """
CREATE TABLE {name} (
    normalized_id TEXT PRIMARY KEY,
    trade_id TEXT NOT NULL,
    sensitivity_type TEXT NOT NULL,
    sensitivity_id TEXT NOT NULL,
    original_local REAL NOT NULL,
    approved_adjustment_local REAL NOT NULL,
    adjusted_local REAL NOT NULL,
    fx_rate REAL NOT NULL,
    normalized_usd REAL NOT NULL,
    currency TEXT NOT NULL
)
"""

NORMALIZED_SENSITIVITY_COLUMNS = (
    "normalized_id",
    "trade_id",
    "sensitivity_type",
    "sensitivity_id",
    "original_local",
    "approved_adjustment_local",
    "adjusted_local",
    "fx_rate",
    "normalized_usd",
    "currency",
)

ADJUSTMENTS_DDL = """
CREATE TABLE {name} (
    adjustment_id TEXT PRIMARY KEY,
    sensitivity_id TEXT NOT NULL,
    adjustment_amount_local REAL NOT NULL,
    approval_status TEXT NOT NULL,
    currency TEXT NOT NULL
)
"""

ADJUSTMENTS_COLUMNS = (
    "adjustment_id",
    "sensitivity_id",
    "adjustment_amount_local",
    "approval_status",
    "currency",
)

RAW_SENSITIVITY_DDL = """
CREATE TABLE {name} (
    sensitivity_id TEXT PRIMARY KEY,
    sensitivity_local REAL NOT NULL,
    currency TEXT NOT NULL
)
"""

RAW_SENSITIVITY_COLUMNS = ("sensitivity_id", "sensitivity_local", "currency")

FX_RATES_DDL = """
CREATE TABLE {name} (
    fx_rate_id TEXT PRIMARY KEY,
    currency TEXT NOT NULL,
    fx_rate REAL NOT NULL
)
"""

FX_RATES_COLUMNS = ("fx_rate_id", "currency", "fx_rate")

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


def _normalized_sensitivity(name: str, rows: Sequence[tuple]) -> TableFixture:
    return TableFixture(
        name,
        NORMALIZED_SENSITIVITY_DDL.format(name=name),
        NORMALIZED_SENSITIVITY_COLUMNS,
        tuple(rows),
    )


def _adjustments(name: str, rows: Sequence[tuple]) -> TableFixture:
    return TableFixture(name, ADJUSTMENTS_DDL.format(name=name), ADJUSTMENTS_COLUMNS, tuple(rows))


def _raw_sensitivity(name: str, rows: Sequence[tuple]) -> TableFixture:
    return TableFixture(
        name,
        RAW_SENSITIVITY_DDL.format(name=name),
        RAW_SENSITIVITY_COLUMNS,
        tuple(rows),
    )


def _fx_rates(name: str, rows: Sequence[tuple]) -> TableFixture:
    return TableFixture(name, FX_RATES_DDL.format(name=name), FX_RATES_COLUMNS, tuple(rows))


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
    # ------------------------------------------------------------------
    # Scenario B: normalized sensitivity
    #
    # normalized_usd = adjusted_local * fx_rate
    # adjusted_local = original_local + approved_adjustment_local
    #
    # Every row but NORM-SENS-00005 reconciles exactly. NORM-SENS-00005 keeps
    # the value published on the sheet (3.18) even though the declared formula
    # produces 460.23 * 0.01 = 4.6023; the trace flags the mismatch instead of
    # rewriting the reported figure.
    # Join/filter keys (sensitivity_id, fx_rate_id) are context, not nodes, but
    # the approval gate is published as a real dependency of the adjustment.
    # ------------------------------------------------------------------
    "normalized_db": SystemFixture(
        definition=SYSTEMS["normalized_db"],
        tables=(
            _normalized_sensitivity(
                "normalized_sensitivity",
                (
                    ("NORM-SENS-00001", "TRD-0001", "DELTA", "SENS-00001",
                     40612.02, -40.02, 40572.00, 0.01, 405.72, "EUR"),
                    ("NORM-SENS-00002", "TRD-0002", "DELTA", "SENS-00002",
                     56239.02, -40.02, 56199.00, 0.01, 561.99, "EUR"),
                    ("NORM-SENS-00003", "TRD-0002", "VEGA", "SENS-00003",
                     3787.02, -40.02, 3747.00, 0.01, 37.47, "EUR"),
                    ("NORM-SENS-00004", "TRD-0002", "CURVATURE", "SENS-00004",
                     789.02, -40.02, 749.00, 0.01, 7.49, "EUR"),
                    ("NORM-SENS-00005", "TRD-0003", "DELTA", "SENS-00005",
                     500.25, -40.02, 460.23, 0.01, 3.18, "EUR"),
                    ("NORM-SENS-00006", "TRD-0003", "VEGA", "SENS-00006",
                     63.02, -40.02, 23.00, 0.01, 0.23, "EUR"),
                    ("NORM-SENS-00007", "TRD-0003", "CURVATURE", "SENS-00007",
                     45.02, -40.02, 5.00, 0.01, 0.05, "EUR"),
                    ("NORM-SENS-00008", "TRD-0004", "DELTA", "SENS-00008",
                     72040.02, -40.02, 72000.00, 0.01, 720.00, "EUR"),
                    ("NORM-SENS-00009", "TRD-0005", "DELTA", "SENS-00009",
                     65665.02, -40.02, 65625.00, 0.01, 656.25, "EUR"),
                    ("NORM-SENS-00010", "TRD-0006", "DELTA", "SENS-00010",
                     83032.02, -40.02, 82992.00, 0.01, 829.92, "EUR"),
                ),
            ),
        ),
        metadata=(
            MetadataRow(
                table_name="normalized_sensitivity",
                attribute_name="normalized_usd",
                sources=(
                    "normalized_db.normalized_sensitivity.adjusted_local",
                    "fx_reference.fx_rates.fx_rate",
                ),
                transformation="adjusted_local * fx_rate",
                consumed_by="normalized sensitivity sheet",
            ),
            MetadataRow(
                table_name="normalized_sensitivity",
                attribute_name="adjusted_local",
                sources=(
                    "raw_sensitivity.raw_sensitivity.sensitivity_local",
                    "adjustment_db.adjustments.adjustment_amount_local",
                ),
                transformation="original_local + approved_adjustment_local",
                consumed_by="normalized_db.normalized_sensitivity.normalized_usd",
            ),
        ),
    ),
    "adjustment_db": SystemFixture(
        definition=SYSTEMS["adjustment_db"],
        tables=(
            _adjustments(
                "adjustments",
                (
                    ("ADJ-00001", "SENS-00001", -40.02, "APPROVED", "EUR"),
                    ("ADJ-00002", "SENS-00002", -40.02, "APPROVED", "EUR"),
                    ("ADJ-00003", "SENS-00003", -40.02, "APPROVED", "EUR"),
                    ("ADJ-00004", "SENS-00004", -40.02, "APPROVED", "EUR"),
                    ("ADJ-00005", "SENS-00005", -40.02, "APPROVED", "EUR"),
                    ("ADJ-00006", "SENS-00006", -40.02, "APPROVED", "EUR"),
                    ("ADJ-00007", "SENS-00007", -40.02, "APPROVED", "EUR"),
                    ("ADJ-00008", "SENS-00008", -40.02, "APPROVED", "EUR"),
                    ("ADJ-00009", "SENS-00009", -40.02, "APPROVED", "EUR"),
                    ("ADJ-00010", "SENS-00010", -40.02, "APPROVED", "EUR"),
                ),
            ),
        ),
        metadata=(
            MetadataRow(
                table_name="adjustments",
                attribute_name="adjustment_amount_local",
                sources=("adjustment_db.adjustments.approval_status",),
                transformation="Apply adjustment only when approval_status = APPROVED",
                consumed_by="normalized_db.normalized_sensitivity.adjusted_local",
            ),
            MetadataRow(
                table_name="adjustments",
                attribute_name="approval_status",
                sources=(),
                transformation="Approved adjustment record",
                consumed_by="normalized_db.normalized_sensitivity.adjusted_local",
            ),
        ),
    ),
    "raw_sensitivity": SystemFixture(
        definition=SYSTEMS["raw_sensitivity"],
        tables=(
            _raw_sensitivity(
                "raw_sensitivity",
                (
                    ("SENS-00001", 40612.02, "EUR"),
                    ("SENS-00002", 56239.02, "EUR"),
                    ("SENS-00003", 3787.02, "EUR"),
                    ("SENS-00004", 789.02, "EUR"),
                    ("SENS-00005", 500.25, "EUR"),
                    ("SENS-00006", 63.02, "EUR"),
                    ("SENS-00007", 45.02, "EUR"),
                    ("SENS-00008", 72040.02, "EUR"),
                    ("SENS-00009", 65665.02, "EUR"),
                    ("SENS-00010", 83032.02, "EUR"),
                ),
            ),
        ),
        metadata=(
            MetadataRow(
                table_name="raw_sensitivity",
                attribute_name="sensitivity_local",
                sources=(),
                transformation="Original reported value",
                consumed_by="normalized_db.normalized_sensitivity.adjusted_local",
            ),
        ),
    ),
    "fx_reference": SystemFixture(
        definition=SYSTEMS["fx_reference"],
        tables=(
            _fx_rates(
                "fx_rates",
                (("FX-EUR-USD", "EUR", 0.01),),
            ),
        ),
        metadata=(
            MetadataRow(
                table_name="fx_rates",
                attribute_name="fx_rate",
                sources=(),
                transformation="Select the applicable currency conversion rate",
                consumed_by="normalized_db.normalized_sensitivity.normalized_usd",
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