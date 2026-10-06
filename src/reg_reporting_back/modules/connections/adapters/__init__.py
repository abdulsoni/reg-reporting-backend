"""Adapter registry.

Adding a database engine means adding one adapter and listing it here; nothing
else in the service needs to change.
"""

from pathlib import Path
from typing import Any

from ....core.exceptions import SystemConnectionError
from .base import DatabaseAdapter
from .mssql import SqlServerAdapter, mssql_adapter_from_params
from .sqlite import SQLiteAdapter

SQLITE = "sqlite"
MSSQL = "mssql+pyodbc"

_ADAPTERS: dict[str, type[DatabaseAdapter]] = {
    SQLITE: SQLiteAdapter,
    MSSQL: SqlServerAdapter,
}

SUPPORTED_DB_TYPES: tuple[str, ...] = tuple(_ADAPTERS)


def get_adapter_class(db_type: str) -> type[DatabaseAdapter]:
    """Return the adapter class for a db_type, or raise."""

    try:
        return _ADAPTERS[db_type]
    except KeyError as exc:
        raise SystemConnectionError(
            f"Unsupported database type '{db_type}'. "
            f"Supported types: {', '.join(SUPPORTED_DB_TYPES)}."
        ) from exc


def build_adapter(
    db_type: str,
    database: str,
    params: dict[str, Any] | None = None,
    password: str | None = None,
    data_dir: Path | None = None,
) -> DatabaseAdapter:
    """Instantiate the adapter for a registered system connection."""

    values = dict(params or {})
    if password is not None:
        values["password"] = password

    adapter_class = get_adapter_class(db_type)

    if adapter_class is SQLiteAdapter:
        return SQLiteAdapter(_resolve_sqlite_path(database, data_dir))

    if adapter_class is SqlServerAdapter:
        return mssql_adapter_from_params(database, values)

    return adapter_class(database, **values)  # pragma: no cover - future adapter


def _resolve_sqlite_path(database: str, data_dir: Path | None) -> Path:
    """Map a registered database name onto a file inside the data directory.

    Only bare names are accepted, so a request can never point the explorer at
    an arbitrary path on the server.
    """

    candidate = Path(database)

    if candidate.is_absolute() or ".." in candidate.parts:
        if data_dir is None:
            raise SystemConnectionError(
                "Absolute SQLite paths are not allowed without a data directory."
            )
        candidate = Path(candidate.name)

    if len(candidate.parts) != 1:
        raise SystemConnectionError(
            f"Invalid SQLite database name '{database}'. "
            "Expected a single file name inside the application data directory."
        )

    return (data_dir or Path(".")) / candidate


__all__ = [
    "MSSQL",
    "SQLITE",
    "SUPPORTED_DB_TYPES",
    "DatabaseAdapter",
    "SQLiteAdapter",
    "SqlServerAdapter",
    "build_adapter",
    "get_adapter_class",
]