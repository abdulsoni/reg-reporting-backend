"""Seed endpoints: build the demo systems and report what is available."""

from fastapi import APIRouter

from ...core.routing import run_operation
from .schema import SeedSystemsResult, SystemInfo
from .service import build_databases, list_systems, reset

router = APIRouter(prefix="/seed", tags=["Seed"])


@router.get("/systems", response_model=list[SystemInfo])
def get_systems() -> list[SystemInfo]:
    """List the systems that can be connected to, and their current state."""

    return run_operation(list_systems)


@router.post("/databases", response_model=SeedSystemsResult)
def create_databases() -> SeedSystemsResult:
    """Create every demo system database from scratch."""

    return run_operation(build_databases)


@router.post("/reset", response_model=SeedSystemsResult)
def reset_everything() -> SeedSystemsResult:
    """Rebuild the demo databases and delete reports, traces and connections."""

    return run_operation(reset)