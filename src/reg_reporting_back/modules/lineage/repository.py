"""Read and write access to lineage traces and their hops.

A trace is a tree: every hop records the path of nodes that led to it, so a
branching lineage stores each node once per path it appears on and the lineage
graph is a pure projection of the hop table.
"""

import json
from dataclasses import dataclass

from sqlalchemy import text

from ...core.database import create_schema, session
from .schema import (
    SOURCE_REACHED,
    TraceHop,
    TraceStatus,
    new_id,
    utc_now,
)

PATH_SEPARATOR = "|"


@dataclass(frozen=True, slots=True)
class Trace:
    """A stored lineage trace."""

    id: str
    report_id: str | None
    datapoint_id: str | None
    origin_system: str
    status: str
    created_at: str
    updated_at: str


def branch_path(nodes: list[str]) -> str:
    """Render the node ids leading to a hop as a single keyable string."""

    return PATH_SEPARATOR + PATH_SEPARATOR.join(nodes)


def parse_branch_path(path: str) -> list[str]:
    return [part for part in path.split(PATH_SEPARATOR) if part]


def node_id(system: str, table: str, attribute: str) -> str:
    return f"{system}.{table}.{attribute}"


TRACE_COLUMNS = """
    id, report_id, datapoint_id, origin_system, status, created_at, updated_at
"""

HOP_COLUMNS = """
    hop_number, branch_path, system_name, table_name, attribute_name,
    transformation, source_from, source_refs_json, consumed_by, status
"""


class TraceRepository:

    @staticmethod
    def create(report_id: str | None, datapoint_id: str | None, origin_system: str) -> Trace:
        create_schema()
        trace_id = new_id("trace")
        now = utc_now()

        with session() as conn:
            conn.execute(
                text(
                    f"""
                    INSERT INTO lineage_traces ({TRACE_COLUMNS})
                    VALUES (:id, :report_id, :datapoint_id, :origin_system, :status, :now, :now)
                    """
                ),
                {
                    "id": trace_id,
                    "report_id": report_id,
                    "datapoint_id": datapoint_id,
                    "origin_system": origin_system,
                    "status": TraceStatus.PENDING.value,
                    "now": now,
                },
            )

        return Trace(
            id=trace_id,
            report_id=report_id,
            datapoint_id=datapoint_id,
            origin_system=origin_system,
            status=TraceStatus.PENDING.value,
            created_at=now,
            updated_at=now,
        )

    @staticmethod
    def get(trace_id: str) -> Trace | None:
        create_schema()
        with session() as conn:
            row = conn.execute(
                text(
                    f"SELECT {TRACE_COLUMNS} FROM lineage_traces WHERE id = :id"
                ),
                {"id": trace_id},
            ).mappings().first()

        return Trace(**dict(row)) if row else None

    @staticmethod
    def set_status(trace_id: str, status: TraceStatus) -> None:
        with session() as conn:
            conn.execute(
                text(
                    """
                    UPDATE lineage_traces SET status = :status, updated_at = :now
                    WHERE id = :id
                    """
                ),
                {"id": trace_id, "status": status.value, "now": utc_now()},
            )

    @staticmethod
    def save_hop(trace_id: str, hop: TraceHop) -> None:
        """Insert a hop. The (trace_id, branch_path) index makes it idempotent."""

        with session() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO lineage_hops (
                        trace_id, hop_number, branch_path, system_name, table_name,
                        attribute_name, transformation, source_from, source_refs_json,
                        consumed_by, status
                    ) VALUES (
                        :trace_id, :hop_number, :branch_path, :system_name, :table_name,
                        :attribute_name, :transformation, :source_from, :source_refs_json,
                        :consumed_by, :status
                    )
                    ON CONFLICT (trace_id, branch_path) DO UPDATE SET
                        status = excluded.status,
                        source_from = excluded.source_from,
                        source_refs_json = excluded.source_refs_json
                    """
                ),
                {
                    "trace_id": trace_id,
                    "hop_number": hop.hop,
                    "branch_path": hop.branch_path,
                    "system_name": hop.system,
                    "table_name": hop.table_name,
                    "attribute_name": hop.attribute_name,
                    "transformation": hop.transformation,
                    "source_from": hop.source_from,
                    "source_refs_json": json.dumps(hop.sources),
                    "consumed_by": hop.consumed_by,
                    "status": hop.status,
                },
            )

    @staticmethod
    def get_hops(trace_id: str) -> list[TraceHop]:
        create_schema()
        with session() as conn:
            rows = conn.execute(
                text(
                    f"""
                    SELECT {HOP_COLUMNS} FROM lineage_hops
                    WHERE trace_id = :trace_id
                    ORDER BY hop_number, id
                    """
                ),
                {"trace_id": trace_id},
            ).mappings().all()

        return [
            TraceHop(
                hop=row["hop_number"],
                branch_path=row["branch_path"],
                system=row["system_name"],
                table_name=row["table_name"],
                attribute_name=row["attribute_name"],
                transformation=row["transformation"],
                source_from=row["source_from"],
                sources=json.loads(row["source_refs_json"] or "[]"),
                consumed_by=row["consumed_by"],
                status=row["status"],
                is_final_source=row["status"] == SOURCE_REACHED,
            )
            for row in rows
        ]