"""Lineage endpoints: explore a column, then trace it back to its source."""

from fastapi import APIRouter

from ...core.routing import run_operation
from .schema import (
    ExploreRequest,
    ExploreResult,
    LineageGraph,
    TraceHop,
    TraceRequest,
    TraceResult,
)
from .service import LineageService

router = APIRouter(prefix="/lineage", tags=["Lineage"])


@router.post("/explore", response_model=ExploreResult)
def explore_attribute(payload: ExploreRequest) -> ExploreResult:
    """Describe one attribute of a connected system and where it came from."""

    return run_operation(lambda: LineageService.explore(payload))


@router.get("/explore", response_model=ExploreResult)
def explore_attribute_by_query(
    system: str,
    table: str,
    attribute: str,
    report_code: str | None = None,
    row_code: str | None = None,
    column_code: str | None = None,
) -> ExploreResult:
    """Query-parameter form of ``POST /lineage/explore``, for direct links."""

    return run_operation(
        lambda: LineageService.explore(
            ExploreRequest(
                system=system,
                table=table,
                attribute=attribute,
                report_code=report_code,
                row_code=row_code,
                column_code=column_code,
            )
        )
    )


@router.post("/trace", response_model=TraceResult)
def start_trace(payload: TraceRequest) -> TraceResult:
    """Trace a report datapoint back through every connected system.

    Pass ``trace_id`` to resume a trace that was blocked waiting for a system
    connection.
    """

    return run_operation(lambda: LineageService.trace(payload))


@router.get("/trace/{trace_id}", response_model=TraceResult)
def get_trace(trace_id: str) -> TraceResult:
    """Return a stored trace with every hop recorded so far."""

    return run_operation(lambda: LineageService.get_trace(trace_id))


@router.get("/trace/{trace_id}/hops", response_model=list[TraceHop])
def get_trace_hops(trace_id: str) -> list[TraceHop]:
    """Return the hop table of a stored trace."""

    return run_operation(lambda: LineageService.get_trace(trace_id).hops)


@router.get("/trace/{trace_id}/graph", response_model=LineageGraph)
def get_trace_graph(trace_id: str) -> LineageGraph:
    """Return the traced lineage as React Flow nodes and edges."""

    return run_operation(lambda: LineageService.get_graph(trace_id))