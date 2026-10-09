"""Read-only SQLite adapter used by the POC system databases.

The file is opened in SQLite's read-only URI mode, so the adapter cannot write
to a system of record even if a future refactor introduced a mutating query.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ....core.exceptions import SystemConnectionError
from .base import (
    METADATA_TABLE,
    ColumnMetadata,
    DatabaseAdapter,
    MetadataRecord,
    TableMetadata,
    json_safe,
    parse_sources,
    validate_identifier,
)


class SQLiteAdapter(DatabaseAdapter):
    """Read-only access to one of the seeded demo databases."""

    db_type = "sqlite"

    def __init__(self, database: str | Path) -> None:
        self.path = Path(database)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        if not self.path.is_file():
            raise SystemConnectionError(
                f"Database file '{self.path.name}' was not found. "
                "Run 'uv run python scripts/seed_databases.py' to build it."
            )

        uri = f"file:{self.path.as_posix()}?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True)
        except sqlite3.Error as exc:
            raise SystemConnectionError(
                f"Could not open '{self.path.name}': {exc}"
            ) from exc

        conn.row_factory = sqlite3.Row
        try:
            yield conn
        except sqlite3.Error as exc:
            raise SystemConnectionError(
                f"Query against '{self.path.name}' failed: {exc}"
            ) from exc
        finally:
            conn.close()

    def ping(self) -> str:
        with self._connection() as conn:
            tables = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'"
            ).fetchone()[0]
        return f"{self.path.name} ({tables} tables)"

    def list_tables(self, schema: str | None = None) -> list[TableMetadata]:
        del schema  # SQLite has no schemas; the registry is the system name.
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                ORDER BY name
                """
            ).fetchall()

        return [TableMetadata(schema=None, name=row["name"], type="TABLE") for row in rows]

    def list_columns(self, table: str) -> list[ColumnMetadata]:
        target = validate_identifier(table, "table name")
        with self._connection() as conn:
            rows = conn.execute(f"PRAGMA table_info({target})").fetchall()

        if not rows:
            raise SystemConnectionError(f"Table '{target}' was not found.")

        return [
            ColumnMetadata(
                name=row["name"],
                data_type=row["type"] or "UNKNOWN",
                # An INTEGER PRIMARY KEY is the rowid alias and is never null,
                # but SQLite does not set notnull for it.
                is_nullable=not bool(row["notnull"]) and not row["pk"],
                ordinal_position=int(row["cid"]) + 1,
                default=row["dflt_value"],
            )
            for row in rows
        ]

    def sample_rows(
        self,
        table: str,
        row_limit: int,
    ) -> tuple[list[str], list[dict[str, Any]]]:
        target = validate_identifier(table, "table name")
        statement = f"SELECT * FROM {target} LIMIT {int(row_limit)}"

        with self._connection() as conn:
            result = conn.execute(statement)
            columns = [description[0] for description in result.description or ()]
            rows = [
                {key: json_safe(value) for key, value in dict(row).items()}
                for row in result.fetchall()
            ]

        return columns, rows

    def read_column_values(self, table: str, column: str, row_limit: int) -> list[Any]:
        target = validate_identifier(table, "table name")
        attribute = validate_identifier(column, "column name")
        statement = f"SELECT {attribute} FROM {target} LIMIT {int(row_limit)}"

        with self._connection() as conn:
            return [json_safe(row[0]) for row in conn.execute(statement).fetchall()]

    def read_row(
        self,
        table: str,
        key_column: str,
        key_value: Any,
    ) -> dict[str, Any] | None:
        target = validate_identifier(table, "table name")
        key = validate_identifier(key_column, "column name")
        statement = f"SELECT * FROM {target} WHERE {key} = ? LIMIT 1"

        with self._connection() as conn:
            row = conn.execute(statement, (key_value,)).fetchone()

        return {name: json_safe(row[name]) for name in row.keys()} if row else None

    def read_lineage_metadata(
        self,
        table: str,
        attribute: str,
    ) -> list[MetadataRecord]:
        target = validate_identifier(table, "table name")
        name = validate_identifier(attribute, "column name")

        with self._connection() as conn:
            published = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
                (METADATA_TABLE,),
            ).fetchone()

            if published is None:
                return []

            rows = conn.execute(
                f"""
                SELECT table_name, attribute_name, transformation,
                       source_refs_json, consumed_by, status,
                       report_code, row_code, column_code
                FROM {METADATA_TABLE}
                WHERE table_name = ? AND attribute_name = ?
                ORDER BY id
                """,
                (target, name),
            ).fetchall()

        return [
            MetadataRecord(
                table_name=row["table_name"],
                attribute_name=row["attribute_name"],
                transformation=row["transformation"],
                sources=parse_sources(row["source_refs_json"]),
                consumed_by=row["consumed_by"],
                status=row["status"],
                report_code=row["report_code"],
                row_code=row["row_code"],
                column_code=row["column_code"],
            )
            for row in rows
        ]