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