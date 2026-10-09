"""Read-only database adapters used by the lineage explorer.

An adapter is the only way this service reaches a client system. It exposes a
fixed set of introspection and metadata queries and deliberately offers no
generic ``execute``, so no caller can issue DDL or DML against a system of
record.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

import json
import re

from ....core.exceptions import SystemConnectionError

IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

METADATA_TABLE = "lineage_metadata"


def validate_identifier(value: str, label: str = "identifier") -> str:
    """Reject anything that is not a plain SQL identifier.

    Table and column names cannot be bound as parameters, so they are
    interpolated into the statement and must be validated first.
    """

    if not IDENTIFIER_PATTERN.match(value or ""):
        raise SystemConnectionError(
            f"Invalid SQL {label} '{value}'. Only letters, digits and "
            "underscores are allowed."
        )
    return value


def json_safe(value: Any) -> Any:
    """Coerce a driver value into something JSON serialisable."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    return str(value)


def parse_sources(raw: str | None) -> tuple[str, ...]:
    """Read a published source list, tolerating a legacy plain string."""

    if not raw:
        return ()
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return (raw,)
    if isinstance(decoded, str):
        return (decoded,)
    if isinstance(decoded, list):
        return tuple(str(item) for item in decoded)
    return ()


@dataclass(frozen=True, slots=True)
class TableMetadata:
    """A table or view exposed by a system."""

    schema: str | None
    name: str
    type: str


@dataclass(frozen=True, slots=True)
class ColumnMetadata:
    """A single column of a system table."""

    name: str
    data_type: str
    is_nullable: bool
    ordinal_position: int
    default: str | None = None


@dataclass(frozen=True, slots=True)
class MetadataRecord:
    """A lineage relationship a system publishes about one of its columns.

    ``sources`` is empty when the attribute is a system of record.
    """

    table_name: str
    attribute_name: str
    transformation: str | None
    sources: tuple[str, ...]
    consumed_by: str | None
    status: str
    report_code: str | None = None
    row_code: str | None = None
    column_code: str | None = None


class DatabaseAdapter(ABC):
    """Read-only access to one system database."""

    #: Value accepted by ``db_type`` in a connection request.
    db_type: str

    @abstractmethod
    def ping(self) -> str:
        """Open the database and return a human readable target description."""

    @abstractmethod
    def list_tables(self, schema: str | None = None) -> list[TableMetadata]:
        """Return the tables and views of the database."""

    @abstractmethod
    def list_columns(self, table: str) -> list[ColumnMetadata]:
        """Return the columns of a single table."""

    @abstractmethod
    def sample_rows(self, table: str, row_limit: int) -> tuple[list[str], list[dict[str, Any]]]:
        """Return up to ``row_limit`` rows of a table as JSON-safe mappings."""

    @abstractmethod
    def read_column_values(self, table: str, column: str, row_limit: int) -> list[Any]:
        """Return the values of one column, for evidence shown against a hop."""

    @abstractmethod
    def read_row(
        self,
        table: str,
        key_column: str,
        key_value: Any,
    ) -> dict[str, Any] | None:
        """Return one row located by a key column, as a JSON-safe mapping.

        Used to reconcile a reported figure against the source record it was
        derived from. Returns ``None`` when no row matches.
        """

    @abstractmethod
    def read_lineage_metadata(
        self,
        table: str,
        attribute: str,
    ) -> list[MetadataRecord]:
        """Return the lineage rows the system publishes for a column."""