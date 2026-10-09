"""Resolve what a single attribute inside a connected system actually is.

The explorer asks a system two questions:

1. *What does this column contain?* — answered by reading the live table, so
   the hop carries real evidence values rather than only metadata.
2. *Where did it come from?* — answered by the ``lineage_metadata`` rows that
   the system publishes about its own columns.

Resolution is scoped by system + table + attribute + datapoint context, because
a generic column name like ``value`` or ``amount`` exists in many tables of the
same system.
"""

from dataclasses import dataclass
from typing import Any

from ...core.exceptions import (
    AmbiguousMetadataError,
    MetadataError,
    SystemConnectionError,
)
from ..connections.adapters.base import DatabaseAdapter, MetadataRecord
from .schema import (
    AMBIGUOUS_METADATA,
    IDENTIFIED,
    METADATA_NOT_FOUND,
    SOURCE_REACHED,
    DatapointContext,
    ExploreRequest,
    ExploreResult,
)

# How many sample values to read as evidence for a hop.
EVIDENCE_LIMIT = 5

# Actions the UI can react to, mirroring next_action in the spec.
ACTION_TRACE_COMPLETE = "TRACE_COMPLETE"
ACTION_DISAMBIGUATE = "DISAMBIGUATE_DATAPOINT"
ACTION_REVIEW_METADATA = "REVIEW_METADATA"
ACTION_CONTINUE = "CONTINUE_SAME_SYSTEM"
ACTION_CONNECT = "CONNECT_SOURCE"

CONTEXT_FIELDS = ("report_code", "row_code", "column_code")


@dataclass(frozen=True, slots=True)
class SourceReference:
    """A parsed ``system.table.attribute`` reference from a metadata row."""

    system: str
    table: str
    attribute: str

    @property
    def node_id(self) -> str:
        return f"{self.system}.{self.table}.{self.attribute}"


def parse_source_reference(source: str) -> SourceReference | None:
    """Split ``system.table.attribute`` into its parts.

    Returns None when the reference is not in that shape, so a malformed
    lineage row is reported rather than silently followed.
    """

    parts = [part.strip() for part in (source or "").split(".")]

    if len(parts) != 3 or not all(parts):
        return None

    return SourceReference(
        system=parts[0],
        table=parts[1],
        attribute=parts[2],
    )


def _rank(
    record: MetadataRecord,
    context: DatapointContext,
) -> tuple[int, int]:
    """Rank a metadata row against the datapoint context; lower is better.

    Exact matches win, then rows that carry no datapoint context at all
    (they describe the column rather than one report cell), then partial
    matches.

    The comparison is performed field-by-field so optional context fields
    do not cause ``zip(..., strict=True)`` length mismatches.
    """

    requested = {
        field: getattr(context, field)
        for field in CONTEXT_FIELDS
    }

    published = {
        field: getattr(record, field)
        for field in CONTEXT_FIELDS
    }

    # Only fields that were actually supplied by the datapoint are relevant.
    wanted_fields = [
        field
        for field in CONTEXT_FIELDS
        if requested[field] is not None
    ]

    # No datapoint context was supplied.
    if not wanted_fields:
        has_context = any(
            published[field] is not None
            for field in CONTEXT_FIELDS
        )

        return (1, 0) if has_context else (0, 0)

    # Count how many requested fields match the metadata row.
    matches = sum(
        1
        for field in wanted_fields
        if requested[field] == published[field]
    )

    # A metadata row with no context is still preferable to a partially
    # matching contextual row, but an exact match gets the best rank.
    if not any(
        published[field] is not None
        for field in CONTEXT_FIELDS
    ):
        return (1, 0)

    return (
        0 if matches == len(wanted_fields) else 2,
        matches,
    )


def resolve_record(
    records: list[MetadataRecord],
    context: DatapointContext,
    system: str,
    table: str,
    attribute: str,
) -> MetadataRecord:
    """Pick the one metadata row that matches the datapoint context.

    Raises:
        MetadataError: the system publishes no lineage row for the column.
        AmbiguousMetadataError: several rows match equally well.
    """

    if not records:
        raise MetadataError(
            f"System '{system}' publishes no lineage metadata for "
            f"'{table}.{attribute}'."
        )

    best_rank, best = min(
        ((_rank(record, context), record) for record in records),
        key=lambda item: item[0],
    )

    contenders = [
        record
        for record in records
        if _rank(record, context) == best_rank
    ]

    if len(contenders) > 1:
        options = "; ".join(
            f"report={record.report_code} "
            f"row={record.row_code} "
            f"column={record.column_code}"
            for record in contenders
        )

        raise AmbiguousMetadataError(
            f"'{table}.{attribute}' in '{system}' is ambiguous: "
            f"{len(contenders)} metadata rows match equally well "
            f"({options}). Pass report_code, row_code and column_code "
            "to disambiguate."
        )

    return best


def parse_sources(
    sources: tuple[str, ...] | list[str],
) -> tuple[list[SourceReference], list[str]]:
    """Parse source references into lineage nodes.

    Valid source format:

        system.table.attribute

    Invalid references are returned in ``unparsed`` instead of raising.
    """

    targets: list[SourceReference] = []
    unparsed: list[str] = []

    for raw_source in sources:
        if not raw_source:
            continue

        source = str(raw_source).strip()

        if not source:
            continue

        # ----------------------------------------------------------
        # Split only twice.
        #
        # This allows attribute names to contain dots if needed.
        # ----------------------------------------------------------

        parts = source.split(".", 2)

        if len(parts) != 3:
            unparsed.append(source)
            continue

        system, table, attribute = (
            part.strip()
            for part in parts
        )

        if not system or not table or not attribute:
            unparsed.append(source)
            continue

        try:
            targets.append(
                SourceReference(
                    system=system,
                    table=table,
                    attribute=attribute,
                )
            )
        except (ValueError, TypeError):
            unparsed.append(source)

    return targets, unparsed


def outstanding_systems(
    references: list[SourceReference],
    current_system: str,
) -> list[str]:
    """Systems referenced by sources that the user still has to connect."""

    targets = dict.fromkeys(
        reference.system
        for reference in references
    )

    return [
        name
        for name in targets
        if name != current_system
    ]


def evidence_values(
    adapter: DatabaseAdapter,
    table: str,
    attribute: str,
) -> list[Any]:
    """Read a few live values of the column so the hop carries evidence."""

    try:
        return adapter.read_column_values(
            table,
            attribute,
            EVIDENCE_LIMIT,
        )
    except SystemConnectionError:
        return []


def explore(
    adapter: DatabaseAdapter,
    request: ExploreRequest,
    system_name: str,
) -> ExploreResult:
    """Resolve one attribute of one connected system."""

    values = evidence_values(
        adapter,
        request.table,
        request.attribute,
    )

    try:
        record = resolve_record(
            adapter.read_lineage_metadata(
                request.table,
                request.attribute,
            ),
            request.context,
            system_name,
            request.table,
            request.attribute,
        )

    except AmbiguousMetadataError:
        return ExploreResult(
            status=AMBIGUOUS_METADATA,
            system=system_name,
            table_name=request.table,
            attribute_name=request.attribute,
            next_action=ACTION_DISAMBIGUATE,
            evidence_values=values,
        )

    except MetadataError:
        return ExploreResult(
            status=METADATA_NOT_FOUND,
            system=system_name,
            table_name=request.table,
            attribute_name=request.attribute,
            next_action=ACTION_REVIEW_METADATA,
            evidence_values=values,
        )

    # A transformation can depend on several attributes, so every published
    # input is considered when deciding what the UI should connect next. The
    # full published ``sources`` list is returned below, and ``source_from``
    # keeps naming the first input.
    parsed, unparsed = parse_sources(record.sources)

    pending = outstanding_systems(
        parsed,
        system_name,
    )

    if not record.sources:
        status, action = (
            SOURCE_REACHED,
            ACTION_TRACE_COMPLETE,
        )

    elif unparsed:
        status, action = (
            METADATA_NOT_FOUND,
            ACTION_REVIEW_METADATA,
        )

    elif not pending:
        status, action = (
            IDENTIFIED,
            ACTION_CONTINUE,
        )

    else:
        status, action = (
            IDENTIFIED,
            ACTION_CONNECT,
        )

    return ExploreResult(
        status=status,
        system=system_name,
        table_name=record.table_name,
        attribute_name=record.attribute_name,
        transformation=record.transformation,
        source_from=(
            record.sources[0]
            if record.sources
            else None
        ),
        sources=list(record.sources),
        consumed_by=record.consumed_by,
        next_system=(
            pending[0]
            if pending
            else None
        ),
        next_action=action,
        evidence_values=values,
    )