"""Lineage service: explore a single attribute, then trace to the source.

The trace is a breadth-first walk over a graph whose nodes are
``system.table.attribute`` triples and whose edges are the ``source_from``
relationships each system publishes.

There are two trace operations:

1. Start a new trace
   ------------------
   A report datapoint always starts from ``reporting_db``.

       reporting_db
            |
            v
       sa_engine
            |
            v
           dwh
            |
            v
       source system

2. Resume an existing trace
   ------------------------
   The client only provides ``trace_id`` and the system that has now been
   connected.

   The service looks at the existing trace and automatically finds the
   exact ``system.table.attribute`` nodes that were waiting for that system.

   Example:

       reporting_db.c0700_facts.value
                    |
                    v
       sa_engine.calc_sa_exposure.exposure_value
                    |
                    v
       dwh.mart_sa_exposure.off_bal_eur

   If DWH was previously disconnected, the resume request can simply be:

       {
           "system": "dwh",
           "trace_id": "...",
           "datapoint_id": "..."
       }

   The service discovers ``dwh.mart_sa_exposure.off_bal_eur`` from the
   stored trace and continues from there.

The trace only steps into a system the user has already connected. Anything
else is parked in ``pending_systems`` so the UI can ask for the connection.
"""

from collections import deque
from dataclasses import dataclass, field
from typing import Any

from ...core.config import settings
from ...core.exceptions import AmbiguousMetadataError, TraceError
from ...core.systems import entry_reference, get_system
from ..connections.adapters import DatabaseAdapter
from ..connections.repository import ConnectionRepository
from ..connections.service import ConnectionService
from ..reports.repository import ReportRepository
from .explorer import (
    DatapointContext,
    SourceReference,
    explore,
    outstanding_systems,
    parse_sources,
    resolve_record,
)
from .repository import (
    Trace,
    TraceRepository,
    branch_path,
    node_id,
    parse_branch_path,
)
from .schema import (
    IDENTIFIED,
    ExploreRequest,
    ExploreResult,
    GraphEdge,
    GraphNode,
    HopStatus,
    LineageGraph,
    TraceHop,
    TraceRequest,
    TraceResult,
    TraceStatus,
)

# Vertical spacing between graph rows, in pixels.
NODE_SPACING = 150

REPORTING_SYSTEM = "reporting_db"


@dataclass(frozen=True, slots=True)
class _ResumePoint:
    """A lineage node that can be used to resume an existing trace."""

    reference: SourceReference

    # Existing branch leading to this node.
    path: list[str]


@dataclass(slots=True)
class _Walk:
    """Mutable state of a single traversal."""

    trace_id: str
    origin_system: str

    # Hops discovered during the current walk.
    hops: list[TraceHop] = field(default_factory=list)

    # Nodes visited during the current walk.
    visited: set[str] = field(default_factory=set)

    # Systems that are not connected yet.
    pending: list[str] = field(default_factory=list)

    # Final source nodes discovered during the current walk.
    final_sources: list[str] = field(default_factory=list)

    # Nodes where metadata could not be resolved.
    unresolved: list[str] = field(default_factory=list)

    # First system that needs to be connected.
    next_system: str | None = None

    # Number of loops / depth-limit hits.
    loops: int = 0

    # Reuse adapters during one walk.
    adapters: dict[str, DatabaseAdapter] = field(default_factory=dict)

    def adapter(self, system_name: str) -> DatabaseAdapter | None:
        """Return a reusable adapter for a connected system."""

        if system_name not in self.adapters:
            if not ConnectionRepository.is_connected(system_name):
                return None

            self.adapters[system_name] = (
                ConnectionService.adapter_for(system_name)
            )

        return self.adapters[system_name]

    def park(self, system_name: str) -> None:
        """Park a system until the user connects it."""

        if system_name not in self.pending:
            self.pending.append(system_name)

        if self.next_system is None:
            self.next_system = system_name


class LineageService:
    """Explore attributes and trace their lineage across systems."""

    # ------------------------------------------------------------------
    # EXPLORE
    # ------------------------------------------------------------------

    @staticmethod
    def explore(request: ExploreRequest) -> ExploreResult:
        """Resolve one attribute inside a connected system."""

        return explore(
            ConnectionService.adapter_for(request.system),
            request,
        )

    # ------------------------------------------------------------------
    # TRACE
    # ------------------------------------------------------------------

    @staticmethod
    def trace(request: TraceRequest) -> TraceResult:
        """Start or resume a lineage trace for a report datapoint."""

        datapoint = ReportRepository.get_datapoint(
            request.datapoint_id
        )

        if datapoint is None:
            raise TraceError(
                f"Datapoint '{request.datapoint_id}' was not found. "
                "Upload a report and pick a datapoint first."
            )

        context = DatapointContext(
            report_code=datapoint.report_code,
            row_code=datapoint.row_code,
            column_code=datapoint.column_code,
        )

        # ==============================================================
        # 1. START A NEW TRACE
        # ==============================================================

        if not request.trace_id:
            trace, entry = _start_new_trace(
                request=request,
                datapoint=datapoint,
            )

            walk = _Walk(
                trace_id=trace.id,
                origin_system=trace.origin_system,
            )

            _walk(
                entry,
                walk,
                context,
            )

        # ==============================================================
        # 2. RESUME EXISTING TRACE
        # ==============================================================

        else:
            trace = TraceRepository.get(request.trace_id)

            if trace is None:
                raise TraceError(
                    f"Trace '{request.trace_id}' was not found."
                )

            if trace.datapoint_id != datapoint.id:
                raise TraceError(
                    f"Trace '{trace.id}' belongs to datapoint "
                    f"'{trace.datapoint_id}', not '{datapoint.id}'."
                )

            # Read the existing trace.
            existing_hops = TraceRepository.get_hops(
                trace.id
            )

            # Find all exact nodes in the requested system that were
            # previously discovered but could not be walked because the
            # system was disconnected.
            resume_points = _resume_points(
                existing_hops,
                request.system,
            )

            if not resume_points:
                raise TraceError(
                    f"No pending lineage node was found for system "
                    f"'{request.system}' in trace '{trace.id}'. "
                    "The system may already have been traced, or it is "
                    "not referenced by the existing lineage."
                )

            walk = _Walk(
                trace_id=trace.id,
                origin_system=trace.origin_system,
            )

            # Resume every pending branch belonging to this system.
            #
            # This is important because one system may be referenced by
            # multiple branches.
            for resume_point in resume_points:
                _walk(
                    resume_point.reference,
                    walk,
                    context,
                    initial_path=resume_point.path,
                )

        # ==============================================================
        # 3. SAVE NEW HOPS
        # ==============================================================

        for hop in walk.hops:
            TraceRepository.save_hop(
                trace.id,
                hop,
            )

        # ==============================================================
        # 4. READ THE COMPLETE TRACE
        # ==============================================================

        all_hops = TraceRepository.get_hops(
            trace.id
        )

        # Rebuild the trace state from the complete persisted graph.
        complete_walk = _build_trace_state(
            trace=trace,
            hops=all_hops,
        )

        # Preserve unresolved / loop information from the current walk.
        complete_walk.unresolved.extend(
            item
            for item in walk.unresolved
            if item not in complete_walk.unresolved
        )

        complete_walk.loops += walk.loops

        # ==============================================================
        # 5. DETERMINE STATUS
        # ==============================================================

        status = _status_for(
            complete_walk
        )

        TraceRepository.set_status(
            trace.id,
            status,
        )

        return _to_result(
            trace,
            status,
            complete_walk,
        )

    # ------------------------------------------------------------------
    # GET TRACE
    # ------------------------------------------------------------------

    @staticmethod
    def get_trace(trace_id: str) -> TraceResult:
        """Return a stored trace with every hop recorded so far."""

        trace = TraceRepository.get(
            trace_id
        )

        if trace is None:
            raise TraceError(
                f"Trace '{trace_id}' was not found."
            )

        hops = TraceRepository.get_hops(
            trace_id
        )

        walk = _build_trace_state(
            trace=trace,
            hops=hops,
        )

        return _to_result(
            trace,
            TraceStatus(trace.status),
            walk,
        )

    # ------------------------------------------------------------------
    # GRAPH
    # ------------------------------------------------------------------

    @staticmethod
    def get_graph(trace_id: str) -> LineageGraph:
        """Project a trace's hops into React Flow nodes and edges.

        The graph endpoint must never fail just because one metadata source
        reference is malformed or cannot be parsed.

        Valid lineage references look like:

            system.table.attribute

        Invalid/unparsed references are simply skipped from the graph and
        remain visible in the hop/source information returned by the trace API.
        """

        hops = TraceRepository.get_hops(trace_id)

        if not hops:
            raise TraceError(
                f"Trace '{trace_id}' has no hops yet."
            )

        depths: dict[str, int] = {}
        details: dict[str, dict[str, Any]] = {}
        edges: dict[str, GraphEdge] = {}

        for hop in hops:
            identifier = node_id(
                hop.system,
                hop.table_name,
                hop.attribute_name,
            )

            # ----------------------------------------------------------
            # Register the current node.
            # ----------------------------------------------------------

            if (
                identifier not in depths
                or hop.hop < depths[identifier]
            ):
                depths[identifier] = hop.hop

                details[identifier] = {
                    "system": hop.system,
                    "table": hop.table_name,
                    "attribute": hop.attribute_name,
                    "transformation": hop.transformation,
                    "status": hop.status,
                    "final_source": hop.is_final_source,
                    "depth": hop.hop,
                    "pending": False,
                }

            # ----------------------------------------------------------
            # Build edges from source references.
            # ----------------------------------------------------------

            for source in hop.sources or []:
                if not source:
                    continue

                try:
                    targets, _unparsed = parse_sources(
                        (source,)
                    )
                except (IndexError, ValueError, TypeError):
                    # Bad metadata should not break the graph endpoint.
                    #
                    # The original source is still available through
                    # hop.sources in GET /lineage/trace/{trace_id}.
                    continue

                for upstream in targets:
                    if not upstream.node_id:
                        continue

                    edge_id = (
                        f"{upstream.node_id}->{identifier}"
                    )

                    edges[edge_id] = GraphEdge(
                        id=edge_id,
                        source=upstream.node_id,
                        target=identifier,
                    )

        # --------------------------------------------------------------
        # Add upstream nodes that are referenced by an edge but have not
        # yet been walked.
        # --------------------------------------------------------------

        for edge in edges.values():
            if edge.source in depths:
                continue

            target_depth = depths.get(
                edge.target,
                1,
            )

            depths[edge.source] = target_depth + 1

            # Parse the node safely.
            parts = edge.source.split(
                ".",
                2,
            )

            system = parts[0] if len(parts) > 0 else ""
            table = parts[1] if len(parts) > 1 else ""
            attribute = parts[2] if len(parts) > 2 else ""

            details[edge.source] = {
                "system": system,
                "table": table,
                "attribute": attribute,
                "pending": True,
                "status": "PENDING",
                "final_source": False,
                "depth": depths[edge.source],
            }

        # --------------------------------------------------------------
        # Build React Flow nodes.
        # --------------------------------------------------------------

        nodes = [
            GraphNode(
                id=identifier,
                position={
                    "x": 0,
                    "y": -depth * NODE_SPACING,
                },
                data=details[identifier],
            )
            for identifier, depth in depths.items()
        ]

        return LineageGraph(
            nodes=sorted(
                nodes,
                key=lambda node: (
                    -node.data["depth"],
                    node.id,
                ),
            ),
            edges=sorted(
                edges.values(),
                key=lambda edge: edge.id,
            ),
        )


# ======================================================================
# TRACE START
# ======================================================================


def _start_new_trace(
    request: TraceRequest,
    datapoint,
):
    """Create a new report trace.

    A new report trace can only begin from reporting_db.
    """

    if request.system != REPORTING_SYSTEM:
        definition = get_system(
            request.system
        )

        label = (
            definition.label
            if definition
            else request.system
        )

        raise TraceError(
            f"System '{request.system}' ({label}) is not a "
            "reporting layer, so a report datapoint cannot be "
            f"traced from it. Start the trace from {REPORTING_SYSTEM}."
        )

    trace = TraceRepository.create(
        request.report_id or datapoint.report_id,
        datapoint.id,
        REPORTING_SYSTEM,
    )

    entry = _entry_reference(
        datapoint,
        REPORTING_SYSTEM,
    )

    return trace, entry


# ======================================================================
# TRACE RESUME
# ======================================================================


def _resume_points(
    hops: list[TraceHop],
    system: str,
) -> list[_ResumePoint]:
    """Find all pending nodes for a system from an existing trace.

    A hop may contain multiple source references.

    Example:

        sa_engine.calc_sa_exposure.exposure_value
            sources:
                - dwh.mart_sa_exposure.off_bal_eur
                - staging.customer_allocated_value.value

    If DWH is connected, the DWH node can be walked.

    If staging is subsequently connected, the staging node can also be
    walked.

    The existing branch path is preserved so resumed hops remain part of
    the original lineage path.
    """

    points: dict[tuple[str, str], _ResumePoint] = {}

    # Nodes that have already been successfully recorded in the trace.
    walked_nodes = {
        node_id(
            hop.system,
            hop.table_name,
            hop.attribute_name,
        )
        for hop in hops
    }

    for hop in hops:
        if not hop.sources:
            continue

        targets, _unparsed = parse_sources(
            tuple(hop.sources)
        )

        # The current hop's branch path already contains the current node.
        existing_path = parse_branch_path(
            hop.branch_path
        )

        for target in targets:
            if target.system != system:
                continue

            # If the exact target has already been persisted as a hop,
            # it has already been explored successfully. Do not restart it.
            if target.node_id in walked_nodes:
                continue

            key = (
                target.node_id,
                hop.branch_path,
            )

            points[key] = _ResumePoint(
                reference=target,
                path=existing_path,
            )

    return list(points.values())


# ======================================================================
# ENTRY REFERENCE
# ======================================================================


def _entry_reference(
    datapoint,
    system: str,
) -> SourceReference:
    """Work out which column of the starting system the datapoint points at."""

    target = entry_reference(
        system
    )

    if target is None:
        definition = get_system(
            system
        )

        label = (
            definition.label
            if definition
            else system
        )

        raise TraceError(
            f"System '{system}' ({label}) is not a reporting layer, "
            "so a report datapoint cannot be traced from it. "
            "Start the trace from reporting_db."
        )

    table, attribute = target

    return SourceReference(
        system=system,
        table=table,
        attribute=attribute,
    )


# ======================================================================
# WALK
# ======================================================================


def _walk(
    entry: SourceReference,
    walk: _Walk,
    context: DatapointContext | None,
    initial_path: list[str] | None = None,
) -> None:
    """Resolve nodes breadth-first, recording each hop and following inputs.

    ``initial_path`` is empty when starting a new trace.

    When resuming an existing trace, ``initial_path`` contains the already
    persisted path leading to the node being resumed.

    Cycle detection is per path, not global. The same column reached along
    two different branches can therefore be resolved independently.
    """

    queue: deque[
        tuple[SourceReference, list[str]]
    ] = deque(
        [
            (
                entry,
                list(initial_path or []),
            )
        ]
    )

    while queue:
        reference, path = queue.popleft()

        identifier = reference.node_id

        # --------------------------------------------------------------
        # Cycle detection
        # --------------------------------------------------------------

        if identifier in path:
            walk.loops += 1
            continue

        # --------------------------------------------------------------
        # Maximum depth
        # --------------------------------------------------------------

        if len(path) >= settings.MAX_TRACE_DEPTH:
            walk.loops += 1
            continue

        walk.visited.add(
            identifier
        )

        # --------------------------------------------------------------
        # Connection
        # --------------------------------------------------------------

        adapter = walk.adapter(
            reference.system
        )

        if adapter is None:
            walk.park(
                reference.system
            )
            continue

        # --------------------------------------------------------------
        # Read lineage metadata
        # --------------------------------------------------------------

        records = adapter.read_lineage_metadata(
            reference.table,
            reference.attribute,
        )

        if not records:
            walk.unresolved.append(
                identifier
            )
            continue

        # --------------------------------------------------------------
        # Resolve metadata
        # --------------------------------------------------------------

        try:
            record = resolve_record(
                records,
                context,
                reference.system,
                reference.table,
                reference.attribute,
            )

        except AmbiguousMetadataError:
            walk.unresolved.append(
                identifier
            )
            continue

        # --------------------------------------------------------------
        # Determine whether this is a final source
        # --------------------------------------------------------------

        is_source = not record.sources

        hop_number = len(path) + 1

        hop = TraceHop(
            hop=hop_number,
            branch_path=branch_path(
                [
                    *path,
                    identifier,
                ]
            ),
            system=reference.system,
            table_name=record.table_name,
            attribute_name=record.attribute_name,
            transformation=record.transformation,
            source_from=(
                record.sources[0]
                if record.sources
                else None
            ),
            sources=list(
                record.sources
            ),
            consumed_by=record.consumed_by,
            status=(
                HopStatus.SOURCE_REACHED.value
                if is_source
                else IDENTIFIED
            ),
            is_final_source=is_source,
        )

        walk.hops.append(
            hop
        )

        # --------------------------------------------------------------
        # Final source
        # --------------------------------------------------------------

        if is_source:
            if identifier not in walk.final_sources:
                walk.final_sources.append(
                    identifier
                )

            continue

        # --------------------------------------------------------------
        # Follow upstream sources
        # --------------------------------------------------------------

        targets, _unparsed = parse_sources(
            record.sources
        )

        for target in targets:
            queue.append(
                (
                    target,
                    [
                        *path,
                        identifier,
                    ],
                )
            )


# ======================================================================
# BUILD COMPLETE TRACE STATE
# ======================================================================


def _build_trace_state(
    trace: Trace,
    hops: list[TraceHop],
) -> _Walk:
    """Rebuild the current walk state from the persisted trace."""

    walk = _Walk(
        trace_id=trace.id,
        origin_system=trace.origin_system,
        hops=list(hops),
    )

    # --------------------------------------------------------------
    # Final sources
    # --------------------------------------------------------------

    for hop in hops:
        if not hop.is_final_source:
            continue

        identifier = node_id(
            hop.system,
            hop.table_name,
            hop.attribute_name,
        )

        if identifier not in walk.final_sources:
            walk.final_sources.append(
                identifier
            )

    # --------------------------------------------------------------
    # Pending systems
    # --------------------------------------------------------------

    walk.pending = _unconnected_targets(
        hops
    )

    if walk.pending:
        walk.next_system = walk.pending[0]

    return walk


# ======================================================================
# PENDING SYSTEMS
# ======================================================================


def _unconnected_targets(
    hops: list[TraceHop],
) -> list[str]:
    """Return systems referenced by hops that are not connected yet."""

    connected = {
        item.system_name
        for item in ConnectionRepository.list_connected()
    }

    pending: list[str] = []

    for hop in hops:
        if not hop.sources:
            continue

        parsed, _unparsed = parse_sources(
            tuple(hop.sources)
        )

        for name in outstanding_systems(
            parsed,
            hop.system,
        ):
            if (
                name not in connected
                and name not in pending
            ):
                pending.append(
                    name
                )

    return pending


# ======================================================================
# STATUS
# ======================================================================


def _status_for(
    walk: _Walk,
) -> TraceStatus:
    """Determine the overall status of a trace."""

    # A pending connection always takes precedence over completion.
    #
    # Example:
    #
    # reporting_db -> sa_engine -> dwh
    #
    # If one branch already reached a final source but another branch
    # requires DWH, the trace is still CONNECTION_REQUIRED.
    if walk.pending:
        return TraceStatus.CONNECTION_REQUIRED

    # If at least one branch reached a source and no connection is
    # required, the trace is complete.
    if walk.final_sources:
        return TraceStatus.COMPLETED

    # Metadata could not be resolved.
    if walk.unresolved:
        return TraceStatus.METADATA_NOT_FOUND

    # A cycle or maximum-depth condition stopped traversal.
    if walk.loops:
        return TraceStatus.LOOP_DETECTED

    # Nothing has been resolved yet.
    return TraceStatus.PENDING


# ======================================================================
# RESULT
# ======================================================================


def _to_result(
    trace: Trace,
    status: TraceStatus,
    walk: _Walk,
) -> TraceResult:
    """Convert internal trace state into the API response."""

    return TraceResult(
        trace_id=trace.id,
        report_id=trace.report_id,
        datapoint_id=trace.datapoint_id,
        status=status,

        # IMPORTANT:
        # Return the COMPLETE persisted trace, not only the hops discovered
        # during the latest resume request.
        hops=walk.hops,

        next_system=walk.next_system,
        pending_systems=walk.pending,
        final_sources=walk.final_sources,

        # The origin never changes when a trace is resumed.
        current_system=walk.origin_system or None,
    )