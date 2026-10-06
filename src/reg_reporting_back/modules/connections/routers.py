"""Connection endpoints.

Registered under /lineage so the whole lineage workflow lives behind one
prefix, as documented in the README.
"""

from fastapi import APIRouter

from ...core.routing import run_operation
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

router = APIRouter(prefix="/lineage", tags=["Lineage"])


@router.post("/connect", response_model=ConnectResult)
def connect_system(payload: ConnectRequest) -> ConnectResult:
    """Connect a registered system and remember it for later requests."""

    return run_operation(lambda: ConnectionService.connect(payload))


@router.get("/connections", response_model=list[SystemConnectionInfo])
def list_connections() -> list[SystemConnectionInfo]:
    """List the registered system connections."""

    return run_operation(ConnectionService.list_connections)


@router.delete("/connections/{system}", response_model=SystemConnectionInfo)
def disconnect_system(system: str) -> SystemConnectionInfo:
    """Disconnect a system so it must be re-approved before it is explored."""

    return run_operation(lambda: ConnectionService.disconnect(system))


@router.post("/tables", response_model=TableListResult)
def list_tables(payload: TableListRequest) -> TableListResult:
    """List tables and views exposed by a connected system."""

    return run_operation(lambda: ConnectionService.list_tables(payload))


@router.post("/schema", response_model=TableSchemaResult)
def get_table_schema(payload: TableSchemaRequest) -> TableSchemaResult:
    """Describe the columns of a table in a connected system."""

    return run_operation(lambda: ConnectionService.get_schema(payload))


@router.post("/sample", response_model=TableSampleResult)
def get_table_sample(payload: TableSampleRequest) -> TableSampleResult:
    """Fetch a small read-only sample of rows from a table."""

    return run_operation(lambda: ConnectionService.get_sample(payload))