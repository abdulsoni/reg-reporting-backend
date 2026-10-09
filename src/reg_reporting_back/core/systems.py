"""Registry of the source systems that can be explored.

Each system is a separate application with its own database. For the POC every
system is a SQLite file under ``settings.data_dir``, but the registry is the
single place that maps a system name onto a database, so a real deployment only
has to change this module.
"""

import re
from dataclasses import dataclass

SYSTEM_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True, slots=True)
class SystemDefinition:
    """A source system the lineage explorer can connect to."""

    name: str
    db_type: str
    database_name: str
    label: str
    layer: str
    entry_table: str | None = None
    entry_attribute: str | None = None

    @property
    def database_file(self) -> str:
        return f"{self.database_name}.sqlite"

    @property
    def entry_reference(self) -> str | None:
        """The column a report datapoint is read from in this system."""

        if not self.entry_table or not self.entry_attribute:
            return None
        return f"{self.entry_table}.{self.entry_attribute}"


SYSTEMS: dict[str, SystemDefinition] = {
    definition.name: definition
    for definition in (
        SystemDefinition(
            name="reporting_db",
            db_type="sqlite",
            database_name="reporting_db",
            label="Regulatory reporting warehouse",
            layer="reporting",
            # Every reported figure is published through the same generic
            # column, so a datapoint can only be placed in this table by using
            # its report/row/column context.
            entry_table="c0700_facts",
            entry_attribute="value",
        ),
        SystemDefinition(
            name="sa_engine",
            db_type="sqlite",
            database_name="sa_engine",
            label="Standardised approach calculation engine",
            layer="calculation",
        ),
        SystemDefinition(
            name="dwh",
            db_type="sqlite",
            database_name="dwh",
            label="Enterprise data warehouse",
            layer="warehouse",
        ),
        SystemDefinition(
            name="staging",
            db_type="sqlite",
            database_name="staging",
            label="Integration staging layer",
            layer="staging",
        ),
        SystemDefinition(
            name="loans_db",
            db_type="sqlite",
            database_name="loans_db",
            label="Loans booking system of record",
            layer="system_of_record",
        ),
        SystemDefinition(
            name="collateral_db",
            db_type="sqlite",
            database_name="collateral_db",
            label="Collateral system of record",
            layer="system_of_record",
        ),
        # ------------------------------------------------------------------
        # Scenario B: normalized sensitivity
        #
        # A second, independent demo scenario. A datapoint selected from the
        # normalized sensitivity sheet starts here instead of at reporting_db,
        # so this system publishes an entry column like a reporting layer.
        # ------------------------------------------------------------------
        SystemDefinition(
            name="normalized_db",
            db_type="sqlite",
            database_name="normalized_db",
            label="Sensitivity normalization engine",
            layer="calculation",
            entry_table="normalized_sensitivity",
            entry_attribute="normalized_usd",
        ),
        SystemDefinition(
            name="adjustment_db",
            db_type="sqlite",
            database_name="adjustment_db",
            label="Sensitivity adjustment system",
            layer="system_of_record",
        ),
        SystemDefinition(
            name="raw_sensitivity",
            db_type="sqlite",
            database_name="raw_sensitivity",
            label="Raw sensitivity system of record",
            layer="system_of_record",
        ),
        SystemDefinition(
            name="fx_reference",
            db_type="sqlite",
            database_name="fx_reference",
            label="FX reference feed",
            layer="reference",
        ),
    )
}

SYSTEM_NAMES: tuple[str, ...] = tuple(SYSTEMS)


def get_system(name: str) -> SystemDefinition | None:
    """Return the definition for a system name, or None when it is unknown."""

    return SYSTEMS.get((name or "").strip().lower())


def entry_reference(system_name: str) -> tuple[str, str] | None:
    """Return the (table, attribute) a report datapoint maps to in a system."""

    definition = get_system(system_name)
    if definition is None or not definition.entry_table:
        return None
    return definition.entry_table, definition.entry_attribute or ""


def find_system_for_target(
    db_type: str | None,
    database: str | None,
) -> SystemDefinition | None:
    """Resolve a registry system from a client-supplied ``db_type`` + ``database``.

    The database matches either the registry's ``database_name`` (``reporting_db``)
    or its ``database_file`` (``reporting_db.sqlite``), so a client can send
    either. The comparison is case-insensitive and ignores surrounding
    whitespace. When ``db_type`` is omitted the database alone is matched, which
    is unambiguous for the seeded systems because their names are unique.
    """

    wanted_db = (database or "").strip().lower()
    if not wanted_db:
        return None

    wanted_type = (db_type or "").strip().lower()

    for definition in SYSTEMS.values():
        if wanted_type and definition.db_type.lower() != wanted_type:
            continue

        candidates = {
            definition.database_name.lower(),
            definition.database_file.lower(),
        }

        if wanted_db in candidates:
            return definition

    return None