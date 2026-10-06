"""Read-only Microsoft SQL Server adapter.

This carries over the ODBC driver resolution, URL building and error clean-up
from the original connection service, wrapped in the adapter contract so the
explorer never has to know which database engine it is talking to.
"""

import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, time
from decimal import Decimal
from functools import lru_cache
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, URL
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

from ....core.config import settings
from ....core.exceptions import SystemConnectionError
from .base import (
    ColumnMetadata,
    DatabaseAdapter,
    MetadataRecord,
    TableMetadata,
    json_safe,
    parse_sources,
    validate_identifier,
)

DRIVER_PREFERENCE = (
    "ODBC Driver 18 for SQL Server",
    "ODBC Driver 17 for SQL Server",
    "SQL Server Native Client 11.0",
    "SQL Server",
)

TABLE_LIST_SQL = text(
    """
    SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE
    FROM INFORMATION_SCHEMA.TABLES
    WHERE (:schema_filter IS NULL OR TABLE_SCHEMA = :schema_filter)
    ORDER BY TABLE_SCHEMA, TABLE_NAME
    """
)

COLUMN_LIST_SQL = text(
    """
    SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH,
           NUMERIC_PRECISION, NUMERIC_SCALE, IS_NULLABLE,
           ORDINAL_POSITION, COLUMN_DEFAULT
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = :schema_name AND TABLE_NAME = :table_name
    ORDER BY ORDINAL_POSITION
    """
)

METADATA_SQL = text(
    """
    SELECT table_name, attribute_name, transformation,
           source_refs_json, consumed_by, status,
           report_code, row_code, column_code
    FROM lineage_metadata
    WHERE table_name = :table_name AND attribute_name = :attribute_name
    ORDER BY id
    """
)

METADATA_EXISTS_SQL = text(
    """
    SELECT 1 FROM INFORMATION_SCHEMA.TABLES
    WHERE TABLE_NAME = 'lineage_metadata' AND TABLE_TYPE = 'BASE TABLE'
    """
)


@lru_cache(maxsize=1)
def available_odbc_drivers() -> tuple[str, ...]:
    try:
        import pyodbc
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise SystemConnectionError(
            "pyodbc is not installed. Run 'uv sync' to install project dependencies."
        ) from exc

    try:
        return tuple(pyodbc.drivers())
    except Exception as exc:  # pragma: no cover - driver manager failure
        raise SystemConnectionError(
            f"Could not enumerate installed ODBC drivers: {exc}"
        ) from exc


def resolve_mssql_driver() -> str:
    """Return the ODBC driver name to use for SQL Server connections.

    Uses MSSQL_ODBC_DRIVER from the environment when set, otherwise picks the
    best Microsoft SQL Server driver installed on the machine.
    """

    installed = available_odbc_drivers()

    configured = (settings.MSSQL_ODBC_DRIVER or "").strip()
    if configured:
        if configured not in installed:
            raise SystemConnectionError(
                f"Configured ODBC driver '{configured}' is not installed. "
                f"Available drivers: {', '.join(installed) or 'none'}."
            )
        return configured

    for candidate in DRIVER_PREFERENCE:
        if candidate in installed:
            return candidate

    raise SystemConnectionError(
        "No Microsoft SQL Server ODBC driver was found. Install "
        "'ODBC Driver 18 for SQL Server' (or 17), or set MSSQL_ODBC_DRIVER in .env."
    )


def build_connection_url(
    host: str,
    database: str,
    username: str | None = None,
    password: str | None = None,
    port: int | None = 1433,
) -> URL:
    """Build the SQLAlchemy URL, handling host\\INSTANCE and host,port forms."""

    driver = resolve_mssql_driver()

    is_named_instance = "\\" in host or "/" in host or "," in host
    server = f"{host},{port}" if port and not is_named_instance else host

    query: dict[str, str] = {
        "driver": driver,
        "TrustServerCertificate": "yes",
    }
    if not username:
        query["Trusted_Connection"] = "yes"

    return URL.create(
        "mssql+pyodbc",
        username=username,
        password=password,
        host=server,
        database=database,
        query=query,
    )


def clean_message(message: str) -> str:
    """Make a driver error readable in a single-line JSON detail.

    SQLAlchemy wraps pyodbc errors as ``(pyodbc.OperationalError) ('08001',
    '[08001] [ODBC Driver]...' ) (Background on this error at: https://...)``.
    The driver's own message is the actionable part, so we keep the first
    clause of it and drop the repeated tail plus the docs link.
    """

    message = " ".join(message.split())
    message = re.sub(r"\s*\(Background on this error at: https?://\S+\)", "", message).strip()

    match = re.search(r"\('[0-9A-Z]{5}',\s*'(?P<detail>.*)'\s*\)", message)
    if not match:
        return message

    detail = match.group("detail").strip()

    # pyodbc appends one bracketed clause per failure; the first is the root cause.
    first_clause = detail.split(";")[0].strip()
    if first_clause:
        return first_clause

    return message


def qualified_name(schema: str, table: str) -> str:
    """Validate and bracket a schema-qualified table name."""

    validate_identifier(schema, "schema name")
    validate_identifier(table, "table name")
    return f"[{schema}].[{table}]"


class SqlServerAdapter(DatabaseAdapter):
    """Read-only access to a SQL Server database."""

    db_type = "mssql+pyodbc"

    def __init__(
        self,
        database: str,
        host: str,
        username: str | None = None,
        password: str | None = None,
        port: int | None = 1433,
        schema: str | None = None,
    ) -> None:
        self.database = database
        self.host = host
        self.username = username
        self.password = password
        self.port = port
        self.schema = schema or "dbo"

    @contextmanager
    def _connection(self) -> Iterator[Connection]:
        """Open a short-lived connection to the target database.

        A NullPool engine is created and disposed per call, so no client
        database credentials or sockets are held between requests.
        """

        engine = None
        try:
            engine = create_engine(
                build_connection_url(
                    self.host,
                    self.database,
                    username=self.username,
                    password=self.password,
                    port=self.port,
                ),
                poolclass=NullPool,
                pool_pre_ping=True,
                connect_args={"timeout": settings.DB_CONNECT_TIMEOUT_SECONDS},
            )
            with engine.connect() as conn:
                yield conn
        except SQLAlchemyError as exc:
            raise SystemConnectionError(clean_message(str(exc))) from exc
        finally:
            if engine is not None:
                engine.dispose()

    def ping(self) -> str:
        with self._connection() as conn:
            database = conn.execute(text("SELECT DB_NAME()")).scalar_one()
            server = conn.execute(
                text("SELECT CAST(SERVERPROPERTY('ServerName') AS NVARCHAR(255))")
            ).scalar_one()
            version = conn.execute(
                text("SELECT CAST(SERVERPROPERTY('ProductVersion') AS NVARCHAR(255))")
            ).scalar_one()
        return f"{server}/{database} ({version})"

    def list_tables(self, schema: str | None = None) -> list[TableMetadata]:
        with self._connection() as conn:
            rows = conn.execute(
                TABLE_LIST_SQL, {"schema_filter": schema or self.schema}
            ).mappings().all()

        return [
            TableMetadata(
                schema=row["TABLE_SCHEMA"],
                name=row["TABLE_NAME"],
                type=row["TABLE_TYPE"],
            )
            for row in rows
        ]

    def list_columns(self, table: str) -> list[ColumnMetadata]:
        schema = self.schema
        qualified_name(schema, table)

        with self._connection() as conn:
            rows = conn.execute(
                COLUMN_LIST_SQL, {"schema_name": schema, "table_name": table}
            ).mappings().all()

        if not rows:
            raise SystemConnectionError(f"Table '{schema}.{table}' was not found.")

        return [
            ColumnMetadata(
                name=row["COLUMN_NAME"],
                data_type=row["DATA_TYPE"],
                is_nullable=row["IS_NULLABLE"] == "YES",
                ordinal_position=int(row["ORDINAL_POSITION"]),
                default=row["COLUMN_DEFAULT"],
            )
            for row in rows
        ]

    def sample_rows(
        self,
        table: str,
        row_limit: int,
    ) -> tuple[list[str], list[dict[str, Any]]]:
        target = qualified_name(self.schema, table)

        with self._connection() as conn:
            result = conn.execute(text(f"SELECT TOP ({int(row_limit)}) * FROM {target}"))
            columns = list(result.keys())
            rows = [
                {key: json_safe(value) for key, value in row.items()}
                for row in result.mappings().all()
            ]

        return columns, rows

    def read_column_values(self, table: str, column: str, row_limit: int) -> list[Any]:
        validate_identifier(column, "column name")
        target = qualified_name(self.schema, table)

        with self._connection() as conn:
            result = conn.execute(
                text(f"SELECT TOP ({int(row_limit)}) {column} FROM {target}")
            )
            return [json_safe(row[0]) for row in result.fetchall()]

    def read_lineage_metadata(
        self,
        table: str,
        attribute: str,
    ) -> list[MetadataRecord]:
        validate_identifier(table, "table name")
        validate_identifier(attribute, "column name")

        with self._connection() as conn:
            if conn.execute(METADATA_EXISTS_SQL).first() is None:
                return []

            rows = conn.execute(
                METADATA_SQL, {"table_name": table, "attribute_name": attribute}
            ).mappings().all()

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


def mssql_adapter_from_params(
    database: str,
    params: dict[str, Any] | None = None,
) -> SqlServerAdapter:
    """Build a SQL Server adapter from connection params."""

    values = params or {}
    return SqlServerAdapter(
        database=database,
        host=values.get("host", ""),
        username=values.get("username"),
        password=values.get("password"),
        port=values.get("port", 1433),
        schema=values.get("schema"),
    )