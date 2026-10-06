"""Connections module: registering source systems and exploring them read-only."""

from .adapters import SUPPORTED_DB_TYPES, DatabaseAdapter, SQLiteAdapter, SqlServerAdapter
from .repository import ConnectionRepository, SystemConnection
from .schema import (
    ConnectRequest,
    ConnectResult,
    SystemConnectionInfo,
    TableListRequest,
    TableListResult,
    TableSampleRequest,
    TableSampleResult,
    TableSchemaRequest,
    TableSchemaResult,
)
from .service import ConnectionService

__all__ = [
    "SUPPORTED_DB_TYPES",
    "ConnectRequest",
    "ConnectResult",
    "ConnectionRepository",
    "ConnectionService",
    "DatabaseAdapter",
    "SQLiteAdapter",
    "SqlServerAdapter",
    "SystemConnection",
    "SystemConnectionInfo",
    "TableListRequest",
    "TableListResult",
    "TableSampleRequest",
    "TableSampleResult",
    "TableSchemaRequest",
    "TableSchemaResult",
]