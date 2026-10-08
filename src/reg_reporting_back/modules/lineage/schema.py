"""Request and response models for lineage exploration and tracing."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Hop / trace statuses, using the status model from the spec:
# IDENTIFIED / CONNECTION_REQUIRED / SOURCE_REACHED / COMPLETED / FAILED /
# LOOP_DETECTED / METADATA_NOT_FOUND.
IDENTIFIED = "IDENTIFIED"
SOURCE_REACHED = "SOURCE_REACHED"
LOOP_DETECTED = "LOOP_DETECTED"
METADATA_NOT_FOUND = "METADATA_NOT_FOUND"
AMBIGUOUS_METADATA = "AMBIGUOUS_METADATA"


class TraceStatus(StrEnum):
    """Lifecycle of a lineage trace."""

    PENDING = "PENDING"
    CONNECTION_REQUIRED = "CONNECTION_REQUIRED"
    SOURCE_REACHED = "SOURCE_REACHED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    LOOP_DETECTED = "LOOP_DETECTED"
    METADATA_NOT_FOUND = "METADATA_NOT_FOUND"


def new_id(prefix: str) -> str:
    """Generate a short, sortable, URL-safe identifier."""

    return f"{prefix}-{uuid4().hex[:12]}"


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class DatapointContext(BaseModel):
    """The report coordinates that disambiguate a column inside a table."""

    report_code: str | None = None
    row_code: str | None = None
    column_code: str | None = None


class ExploreRequest(BaseModel):
    """Ask the explorer what a single attribute inside a connected system is.

    The target may be named by ``system`` or by ``db_type`` + ``database``.
    """

    model_config = ConfigDict(populate_by_name=True)

    system: str | None = Field(
        default=None,
        description="Connected system name; omit to resolve from db_type + database",
    )
    db_type: str | None = None
    database: str | None = None
    table: str = Field(min_length=1)
    attribute: str = Field(min_length=1)
    report_code: str | None = None
    row_code: str | None = None
    column_code: str | None = None

    @field_validator("system")
    @classmethod
    def _clean_system(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip().lower() or None

    @property
    def context(self) -> DatapointContext:
        return DatapointContext(
            report_code=self.report_code,
            row_code=self.row_code,
            column_code=self.column_code,
        )


class ExploreResult(BaseModel):
    """What the explorer found for one attribute, and what to do next."""

    status: str
    system: str
    table_name: str
    attribute_name: str
    transformation: str | None = None
    source_from: str | None = Field(
        default=None, description="Primary input attribute, or null at a source"
    )
    sources: list[str] = Field(default_factory=list)
    consumed_by: str | None = None
    next_system: str | None = Field(
        default=None, description="System the user must connect to continue"
    )
    next_action: str = Field(description="What the UI should do next")
    evidence_values: list[Any] = Field(
        default_factory=list, description="Sample values read from the live column"
    )


class TraceRequest(BaseModel):
    """Start a lineage trace from a report datapoint, or resume one.

    The target system may be named by ``system`` or resolved from
    ``db_type`` + ``database``.
    """

    system: str | None = None
    db_type: str | None = None
    database: str | None = None
    datapoint_id: str
    report_id: str | None = None
    # Required only when starting
    current_system: str = "reporting_db"
    trace_id: str | None = Field(
        default=None, description="Resume an existing trace instead of starting one"
    )
    table: str | None = None
    attribute: str | None = None

    @field_validator("system")
    @classmethod
    def _clean_system(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip().lower() or None


class HopStatus(StrEnum):
    """Status of a single hop in a lineage trace."""

    IDENTIFIED = IDENTIFIED
    SOURCE_REACHED = SOURCE_REACHED
    LOOP_DETECTED = LOOP_DETECTED
    METADATA_NOT_FOUND = METADATA_NOT_FOUND
    AMBIGUOUS_METADATA = AMBIGUOUS_METADATA


class TraceHop(BaseModel):
    """One resolved system/table/attribute on the path back to a source."""

    hop: int = Field(description="1-based depth from the starting datapoint")
    branch_path: str = Field(description="Pipe-separated node ids, for graph grouping")
    system: str
    table_name: str
    attribute_name: str
    transformation: str | None = None
    source_from: str | None = None
    sources: list[str] = Field(default_factory=list)
    consumed_by: str | None = None
    status: str = IDENTIFIED
    is_final_source: bool = False


class TraceResult(BaseModel):
    """A lineage trace: every hop so far, plus what is needed to continue."""

    trace_id: str
    report_id: str | None = None
    datapoint_id: str | None = None
    status: TraceStatus
    hops: list[TraceHop]
    next_system: str | None = Field(
        default=None, description="Next system to connect, if one is required"
    )
    pending_systems: list[str] = Field(
        default_factory=list, description="All systems blocking the trace"
    )
    final_sources: list[str] = Field(default_factory=list)
    current_system: str | None = None


class GraphNode(BaseModel):
    """A node in the lineage graph, ready for React Flow."""

    id: str
    type: str = "lineage"
    position: dict[str, int]
    data: dict[str, Any]


class GraphEdge(BaseModel):
    """An edge in the lineage graph, ready for React Flow."""

    id: str
    source: str
    target: str
    label: str = "source_from"
    animated: bool = False


class LineageGraph(BaseModel):
    """Nodes and edges describing the traced lineage."""

    nodes: list[GraphNode]
    edges: list[GraphEdge]