"""Lineage module: exploring systems and tracing a value back to its source."""

from .explorer import SourceReference, explore, parse_source_reference, resolve_record
from .repository import Trace, TraceRepository
from .schema import (
    ExploreRequest,
    ExploreResult,
    GraphEdge,
    GraphNode,
    LineageGraph,
    TraceHop,
    TraceRequest,
    TraceResult,
    TraceStatus,
)
from .service import LineageService

__all__ = [
    "ExploreRequest",
    "ExploreResult",
    "GraphEdge",
    "GraphNode",
    "LineageGraph",
    "LineageService",
    "SourceReference",
    "Trace",
    "TraceHop",
    "TraceRepository",
    "TraceRequest",
    "TraceResult",
    "TraceStatus",
    "explore",
    "parse_source_reference",
    "resolve_record",
]