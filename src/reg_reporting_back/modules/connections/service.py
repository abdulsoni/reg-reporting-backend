"""Application service for system connections and read-only exploration."""

from ...core.config import settings
from ...core.exceptions import SystemConnectionError
from ...core.systems import SystemDefinition, find_system_for_target, get_system
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


def _resolve_definition(
    system: str | None,
    db_type: str | None,
    database: str | None,
) -> SystemDefinition:
    """Resolve a request target to a registry system.

    ``system`` wins when supplied; otherwise the ``db_type`` + ``database``
    pair is matched against the registry. Anything unmatched is refused, so a
    request can never point the explorer at an arbitrary system.
    """

    if system:
        definition = get_system(system)
        if definition is None:
            raise SystemConnectionError(
                f"Unknown system '{system}'. "
                "GET /seed/systems lists the systems that can be connected."
            )
        return definition

    definition = find_system_for_target(db_type, database)
    if definition is None:
        raise SystemConnectionError(
            f"No registered system matches db_type '{db_type}' and "
            f"database '{database}'. Pass a known 'system', or a db_type and "
            "database from GET /seed/systems."
        )

    return definition


def _resolve_connection(
    system: str | None,
    db_type: str | None,
    database: str | None,
) -> SystemConnection:
    """Resolve a request target to a system the user has already connected."""

    return _require_registered(_resolve_definition(system, db_type, database).name)


def _normalize_sqlite_database(
    definition: SystemDefinition,
    db_type: str,
    database: str,
) -> str:
    """Store a SQLite target as the on-disk file name.

    A client may name the database by its registry name (``reporting_db``) or
    by its file (``reporting_db.sqlite``); the adapter only understands the file.
    """

    if (
        db_type == "sqlite"
        and database.strip().lower() == definition.database_name.lower()
    ):
        return definition.database_file
    return database.strip()


class ConnectionService:
    """Register systems and explore what they expose."""

    @staticmethod
    def connect(payload: ConnectRequest) -> ConnectResult:
        """Validate a system connection and remember it for later requests.

        The target may be named directly or by ``db_type`` + ``database``; the
        resolved registry system is what gets registered.
        """

        definition = _resolve_definition(
            payload.system, payload.db_type, payload.database
        )

        db_type = payload.db_type or definition.db_type
        database = payload.database or definition.database_file
        database = _normalize_sqlite_database(definition, db_type, database)

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

        return ConnectionService._adapter_for(_require_registered(system_name))

    @staticmethod
    def resolve_system(
        system: str | None,
        db_type: str | None,
        database: str | None,
    ) -> str:
        """Return the registry name a request target resolves to.

        Unlike :meth:`adapter_for` this performs no connection check, so it can
        be used to validate a target before it is connected.
        """

        return _resolve_definition(system, db_type, database).name

    @staticmethod
    def _adapter_for(connection: SystemConnection) -> DatabaseAdapter:
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
        connection = _resolve_connection(
            payload.system, payload.db_type, payload.database
        )
        tables = ConnectionService._adapter_for(connection).list_tables(
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
        connection = _resolve_connection(
            payload.system, payload.db_type, payload.database
        )
        columns = ConnectionService._adapter_for(connection).list_columns(payload.table)

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
        connection = _resolve_connection(
            payload.system, payload.db_type, payload.database
        )
        columns, rows = ConnectionService._adapter_for(connection).sample_rows(
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