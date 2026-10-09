"""Lineage service: explore a single attribute, then trace to the source.

The trace is a breadth-first walk over a dependency graph whose nodes are
``system.table.attribute`` triples and whose edges are the published input
references. A transformation can depend on several attributes, so the walk
follows every input recursively and deduplicates nodes by their fully
qualified identity.

There are two trace operations:

1. Start a new trace
   ------------------
   A report datapoint always starts from ``reporting_db``.

       reporting_db.c0700_facts.value
            |
            v
       sa_engine.calc_sa_exposure.exposure_value
            |
            +-- dwh.mart_sa_exposure.off_bal_eur
            |        |
            |        +-- dwh.dw_exposure.undrawn_eur
            |        +-- dwh.dw_exposure.drawn_eur
            |
            +-- dwh.dw_collateral_alloc.allocated_value

2. Resume an existing trace
   ------------------------
   The client only has to provide ``trace_id``. The system target is
   optional and treated as a hint, never as a filter.

   The service reads the stored trace and automatically resumes every
   ``system.table.attribute`` node that was waiting and is connected now,
   regardless of which system the request names. A request that names no
   system at all still advances the trace.

   If DWH was previously disconnected, the resume request can simply be:

       {
           "trace_id": "...",
           "datapoint_id": "..."
       }

   The service discovers the parked DWH nodes from the stored trace and
   continues from there. When nothing is left to resume the trace is
   returned unchanged instead of failing.

The trace only steps into a system the user has already connected. Anything
else is parked in ``pending_systems`` so the UI can ask for the connection.
"""

import math
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
from .formula import evaluate_arithmetic
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
    Reconciliation,
    TraceHop,
    TraceRequest,
    TraceResult,
    TraceStatus,
)

# Graph layout spacing, in pixels. The graph flows left to right, so the
# columns are spread horizontally (upstream on the left, the report datapoint
# on the right) and the nodes sharing a column are spread vertically. The
# horizontal gap is wider than a node box so boxes and edge labels never
# overlap.
HORIZONTAL_SPACING = 340
VERTICAL_SPACING = 180

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

        system_name = ConnectionService.resolve_system(
            request.system, request.db_type, request.database
        )

        return explore(
            ConnectionService.adapter_for(system_name),
            request,
            system_name,
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

        # The system target is optional. It is only a hint: resolving it is
        # skipped entirely when the request names nothing, so a resume that
        # carries just ``trace_id`` still works. A new trace defaults to
        # reporting_db, the only layer a report datapoint can start from.
        system_name = (
            ConnectionService.resolve_system(
                request.system, request.db_type, request.database
            )
            if request.system or request.db_type or request.database
            else REPORTING_SYSTEM
        )

        # ==============================================================
        # 1. START A NEW TRACE
        # ==============================================================

        if not request.trace_id:
            trace, entry = _start_new_trace(
                request=request,
                datapoint=datapoint,
                system_name=system_name,
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

            # Find every node the previous walk had to park because its
            # system was disconnected. The request's system target is only
            # a hint: the service resumes whatever is connected now, so a
            # request that names the origin (or nothing at all) still
            # advances the trace.
            resume_points = _resume_points(
                existing_hops
            )

            walk = _Walk(
                trace_id=trace.id,
                origin_system=trace.origin_system,
            )

            # Treat every node already stored in the trace as visited, so a
            # resumed branch that joins an existing hop stops there instead
            # of being persisted a second time under a different path.
            walk.visited = {
                node_id(
                    hop.system,
                    hop.table_name,
                    hop.attribute_name,
                )
                for hop in existing_hops
            }

            # Resume every pending node that is connectable now.
            #
            # ``_walk`` parks any node whose system is still disconnected,
            # so walking the full list is safe and leaves the trace
            # unchanged when nothing new is available.
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
            reconciliation=_reconcile(datapoint, all_hops),
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

        datapoint = (
            ReportRepository.get_datapoint(trace.datapoint_id)
            if trace.datapoint_id
            else None
        )

        return _to_result(
            trace,
            TraceStatus(trace.status),
            walk,
            reconciliation=_reconcile(datapoint, hops),
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

        trace = TraceRepository.get(trace_id)
        reconciliation = None
        if trace is not None and trace.datapoint_id:
            reconciliation = _reconcile(
                ReportRepository.get_datapoint(trace.datapoint_id),
                hops,
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
            # Build one edge per published input.
            #
            # The trace follows every input of a transformation, so each
            # source of every hop becomes an upstream edge. Inputs that were
            # not walked (a system still disconnected) become PENDING nodes
            # in the block below.
            # ----------------------------------------------------------

            try:
                targets, _unparsed = parse_sources(
                    tuple(hop.sources)
                )
            except (IndexError, ValueError, TypeError):
                # Bad metadata should not break the graph endpoint.
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
        # Attach the reconciliation to the entry (root) node.
        # --------------------------------------------------------------

        if reconciliation is not None:
            for identifier, depth in depths.items():
                if depth == 1:
                    details[identifier]["reconciliation"] = reconciliation.model_dump()

        # --------------------------------------------------------------
        # Build React Flow nodes.
        # --------------------------------------------------------------

        positions = _layout_positions(depths, edges)

        nodes = [
            GraphNode(
                id=identifier,
                position=positions[identifier],
                data=details[identifier],
            )
            for identifier in depths
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


def _layout_positions(
    depths: dict[str, int],
    edges: dict[str, GraphEdge],
) -> dict[str, dict[str, float]]:
    """Lay the graph out left to right so React Flow does not stack it.

    Nodes are grouped into columns by depth. Upstream nodes (larger depth)
    sit on the left and the report datapoint (depth 1) on the right. Within a
    column, nodes are ordered by the average row of their upstream neighbours,
    which keeps linked nodes close together and reduces edge crossings.
    """

    if not depths:
        return {}

    max_depth = max(depths.values())

    columns: dict[int, list[str]] = {}
    for identifier, depth in depths.items():
        columns.setdefault(depth, []).append(identifier)

    # Edges point upstream -> downstream, so the source is the deeper node.
    upstream: dict[str, list[str]] = {}
    for edge in edges.values():
        if edge.source in depths and edge.target in depths:
            upstream.setdefault(edge.target, []).append(edge.source)

    row_of: dict[str, int] = {}
    ordered: dict[int, list[str]] = {}

    # Order the deepest column first so a node's upstream neighbours already
    # have a row when the shallower columns are arranged.
    for depth in range(max_depth, 0, -1):
        column = sorted(columns.get(depth, []))

        def barycenter(identifier: str) -> float:
            rows = [
                row_of[source]
                for source in upstream.get(identifier, [])
                if source in row_of
            ]
            if not rows:
                return float("inf")
            return sum(rows) / len(rows)

        column.sort(key=lambda identifier: (barycenter(identifier), identifier))
        ordered[depth] = column
        for row, identifier in enumerate(column):
            row_of[identifier] = row

    positions: dict[str, dict[str, float]] = {}
    for depth, column in ordered.items():
        middle = (len(column) - 1) / 2
        x = (max_depth - depth) * HORIZONTAL_SPACING
        for row, identifier in enumerate(column):
            positions[identifier] = {
                "x": x,
                "y": (row - middle) * VERTICAL_SPACING,
            }

    return positions


# ======================================================================
# TRACE START
# ======================================================================


def _start_new_trace(
    request: TraceRequest,
    datapoint,
    system_name: str,
):
    """Create a new trace from the datapoint's entry system.

    Any system that publishes an entry column can start a trace.
    ``reporting_db`` is the default for a regulatory return, but a datapoint
    selected from another entry point (for example the normalized sensitivity
    sheet) starts from that system instead.
    """

    if entry_reference(system_name) is None:
        definition = get_system(system_name)

        label = definition.label if definition else system_name

        raise TraceError(
            f"System '{system_name}' ({label}) is not a "
            "reporting layer, so a report datapoint cannot be "
            f"traced from it. Start the trace from {REPORTING_SYSTEM}."
        )

    trace = TraceRepository.create(
        request.report_id or datapoint.report_id,
        datapoint.id,
        system_name,
    )

    entry = _entry_reference(
        datapoint,
        system_name,
    )

    return trace, entry


# ======================================================================
# TRACE RESUME
# ======================================================================


def _resume_points(
    hops: list[TraceHop],
    system: str | None = None,
) -> list[_ResumePoint]:
    """Find pending nodes from an existing trace that can be resumed.

    Every published input of every hop is a possible resume point, because a
    hop can depend on several attributes across several systems.

    Example:

        sa_engine.calc_sa_exposure.exposure_value
            sources: dwh.mart_sa_exposure.off_bal_eur
                     dwh.dw_collateral_alloc.allocated_value

    When ``system`` is given it filters the points to that system. When it
    is ``None`` every pending node is returned, so a resume can continue
    whatever is connected now without naming the system.

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
        targets, _unparsed = parse_sources(
            tuple(hop.sources)
        )

        # The current hop's branch path already contains the current node.
        existing_path = parse_branch_path(
            hop.branch_path
        )

        for target in targets:
            if system is not None and target.system != system:
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

    Every published input is followed, so the walk builds the full dependency
    graph behind the datapoint rather than a single chain. ``walk.visited``
    deduplicates nodes by their fully qualified identity, which both stops a
    dependency that is shared by several consumers from being recorded twice
    and prevents cycles.
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
        # Deduplicate by fully qualified identity
        #
        # A dependency shared by several consumers is resolved once. On a
        # resume the set is pre-seeded with the nodes already persisted, so
        # a branch that joins an existing hop stops instead of being stored
        # again under a different path.
        # --------------------------------------------------------------

        if identifier in walk.visited:
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
        # Follow every published input
        #
        # A transformation can depend on several attributes (for example
        # ``off_bal_eur + allocated_value``). Each input is an explicit
        # ``system.table.attribute`` reference in the metadata, so the whole
        # dependency graph is followed rather than only the first input.
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
    reconciliation: Reconciliation | None = None,
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

        reconciliation=reconciliation,
    )


# ======================================================================
# RECONCILIATION
# ======================================================================


def _reconcile(
    datapoint,
    hops: list[TraceHop],
) -> Reconciliation | None:
    """Check a reported figure against the formula its source declares.

    Only figures that carry identifiers can be located in a source system, so a
    plain COREP datapoint resolves to ``None``. The root hop's ``transformation``
    is evaluated over the values of the matching source row; the reported value
    is never changed, only compared.
    """

    if datapoint is None or not datapoint.identifiers:
        return None

    root = next(
        (hop for hop in hops if len(parse_branch_path(hop.branch_path)) == 1),
        None,
    )

    if root is None or not root.transformation:
        return None

    if not ConnectionRepository.is_connected(root.system):
        return None

    try:
        adapter = ConnectionService.adapter_for(root.system)
    except Exception:  # pragma: no cover - connection raced away
        return None

    row: dict[str, Any] | None = None
    key: dict[str, str] = {}

    for name, value in datapoint.identifiers.items():
        try:
            candidate = adapter.read_row(root.table_name, name, value)
        except Exception:
            continue
        if candidate is not None:
            row = candidate
            key = {name: value}
            break

    if row is None:
        return None

    derived = evaluate_arithmetic(root.transformation, row)
    expected = datapoint.value

    if derived is None or expected is None:
        return None

    return Reconciliation(
        attribute=root.attribute_name,
        key=key,
        formula=root.transformation,
        expected_value=float(expected),
        derived_value=round(float(derived), 6),
        reconciles=math.isclose(
            float(derived),
            float(expected),
            rel_tol=1e-6,
            abs_tol=1e-6,
        ),
    )