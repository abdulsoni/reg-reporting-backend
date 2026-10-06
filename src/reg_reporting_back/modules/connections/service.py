"""Application service for system connections and read-only exploration."""

from ...core.config import settings
from ...core.exceptions import SystemConnectionError
from ...core.systems import get_system
from .adapters import DatabaseAdapter, build_adapter
from .repository import RESERVED_PARAMS, ConnectionRepository, SystemConnection
from .schema import (
    ColumnInfo,
    ConnectRequest,
    ConnectResult,
    SystemConnectionInfo,
    TableInfo,
    TableListRequest,
    TableListResult,
    TableSampleRequest,
    TableSampleResult,
    TableSchemaRequest,
    TableSchemaResult,
)


def _require_registered(system_name: str) -> SystemConnection:
    connection = ConnectionRepository.get(system_name)
    if connection is None or not connection.connected:
        raise SystemConnectionError(
            f"System '{system_name}' is not connected. "
            f"POST /lineage/connect with {{\"system\": \"{system_name}\"}} first."
        )
    return connection


class ConnectionService:
    """Register systems and explore what they expose."""

    @staticmethod
    def connect(payload: ConnectRequest) -> ConnectResult:
        """Validate a system connection and remember it for later requests."""

        definition = get_system(payload.system)
        if definition is None:
            raise SystemConnectionError(
                f"Unknown system '{payload.system}'. "
                "GET /seed/systems lists the systems that can be connected."
            )

        db_type = payload.db_type or definition.db_type
        database = payload.database or definition.database_file

        params = {key: getattr(payload, key) for key in RESERVED_PARAMS}
        params = {key: value for key, value in params.items() if value is not None}

        adapter = build_adapter(
            db_type,
            database,
            params=params,
            password=payload.get_password(),
            data_dir=settings.data_dir,
        )

        target = adapter.ping()
        tables = adapter.list_tables()

        registered = ConnectionRepository.register(
            system_name=definition.name,
            db_type=db_type,
            database_name=database,
            params=params,
        )

        return ConnectResult(
            system=definition.name,
            db_type=registered.db_type,
            database=registered.database_name,
            target=target,
            table_count=len(tables),
            connected_at=registered.connected_at,
        )

    @staticmethod
    def list_connections() -> list[SystemConnectionInfo]:
        """Return every registered connection and its state."""

        return [
            SystemConnectionInfo(
                system_name=item.system_name,
                db_type=item.db_type,
                database_name=item.database_name,
                connected=item.connected,
                connected_at=item.connected_at,
            )
            for item in ConnectionRepository.list_all()
        ]

    @staticmethod
    def disconnect(system_name: str) -> SystemConnectionInfo:
        """Forget the credentials of a system so it has to be re-approved."""

        if not ConnectionRepository.disconnect(system_name):
            raise SystemConnectionError(f"System '{system_name}' is not registered.")

        return SystemConnectionInfo(
            system_name=system_name,
            db_type="",
            database_name="",
            connected=False,
        )

    @staticmethod
    def adapter_for(system_name: str) -> DatabaseAdapter:
        """Return a read-only adapter for a system the user has connected."""

        connection = _require_registered(system_name)
        params = {**connection.params}
        if (table_schema := params.pop("table_schema", None)) is not None:
            params["schema"] = table_schema
        return build_adapter(
            connection.db_type,
            connection.database_name,
            params=params,
            data_dir=settings.data_dir,
        )

    @staticmethod
    def list_tables(payload: TableListRequest) -> TableListResult:
        connection = _require_registered(payload.system)
        tables = ConnectionService.adapter_for(payload.system).list_tables(
            schema=payload.get_schema_name()
        )

        return TableListResult(
            system=connection.system_name,
            database=connection.database_name,
            table_schema=tables[0].schema if tables else payload.get_schema_name(),
            count=len(tables),
            tables=[
                TableInfo(
                    table_schema=table.schema,
                    name=table.name,
                    type=table.type,
                )
                for table in tables
            ],
        )

    @staticmethod
    def get_schema(payload: TableSchemaRequest) -> TableSchemaResult:
        connection = _require_registered(payload.system)
        columns = ConnectionService.adapter_for(payload.system).list_columns(payload.table)

        return TableSchemaResult(
            system=connection.system_name,
            database=connection.database_name,
            table_schema=payload.get_schema_name(),
            table=payload.table,
            count=len(columns),
            columns=[
                ColumnInfo(
                    name=column.name,
                    data_type=column.data_type,
                    is_nullable=column.is_nullable,
                    ordinal_position=column.ordinal_position,
                    default=column.default,
                )
                for column in columns
            ],
        )

    @staticmethod
    def get_sample(payload: TableSampleRequest) -> TableSampleResult:
        connection = _require_registered(payload.system)
        columns, rows = ConnectionService.adapter_for(payload.system).sample_rows(
            payload.table, payload.row_limit
        )

        return TableSampleResult(
            system=connection.system_name,
            database=connection.database_name,
            table_schema=payload.get_schema_name(),
            table=payload.table,
            row_count=len(rows),
            columns=columns,
            rows=rows,
        )