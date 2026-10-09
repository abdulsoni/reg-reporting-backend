"""Read and write access to reports and their datapoints."""

import json
from dataclasses import dataclass, field

from sqlalchemy import text

from ...core.database import session

REPORT_COLUMNS = """
    id, file_name, report_type, report_code, report_title, source_dataset,
    reporting_entity, reporting_date, submission_version, currency,
    page_count, table_count, created_at
"""

DATAPOINT_COLUMNS = """
    id, report_id, report_code, report_title, row_code, column_code,
    attribute_name, value, unit, currency, identifiers_json
"""


@dataclass(frozen=True, slots=True)
class Report:
    """A stored report."""

    id: str
    file_name: str
    report_type: str | None
    report_code: str | None
    report_title: str | None
    source_dataset: str | None
    reporting_entity: str | None
    reporting_date: str | None
    submission_version: str | None
    currency: str | None
    page_count: int
    table_count: int
    created_at: str


@dataclass(frozen=True, slots=True)
class Datapoint:
    """One reported figure belonging to a report."""

    id: str
    report_id: str
    report_code: str | None
    report_title: str | None
    row_code: str | None
    column_code: str | None
    attribute_name: str | None
    value: float | None
    unit: str | None
    currency: str | None
    identifiers: dict[str, str] = field(default_factory=dict)


def _identifiers_from_json(raw: str | None) -> dict[str, str]:
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(decoded, dict):
        return {}
    return {str(key): str(value) for key, value in decoded.items() if value is not None}


class ReportRepository:

    @staticmethod
    def create(report: Report, raw_json: str | None, pdf_bytes: bytes | None) -> Report:
        with session() as conn:
            conn.execute(
                text(
                    f"""
                    INSERT INTO reports (
                        {REPORT_COLUMNS}, raw_json, pdf_bytes
                    ) VALUES (
                        :id, :file_name, :report_type, :report_code, :report_title,
                        :source_dataset, :reporting_entity, :reporting_date,
                        :submission_version, :currency, :page_count, :table_count,
                        :created_at, :raw_json, :pdf_bytes
                    )
                    """
                ),
                {
                    "id": report.id,
                    "file_name": report.file_name,
                    "report_type": report.report_type,
                    "report_code": report.report_code,
                    "report_title": report.report_title,
                    "source_dataset": report.source_dataset,
                    "reporting_entity": report.reporting_entity,
                    "reporting_date": report.reporting_date,
                    "submission_version": report.submission_version,
                    "currency": report.currency,
                    "page_count": report.page_count,
                    "table_count": report.table_count,
                    "created_at": report.created_at,
                    "raw_json": raw_json,
                    "pdf_bytes": pdf_bytes,
                },
            )
        return report

    @staticmethod
    def list_all() -> list[Report]:
        with session() as conn:
            rows = conn.execute(
                text(f"SELECT {REPORT_COLUMNS} FROM reports ORDER BY created_at DESC, id")
            ).mappings().all()
        return [Report(**dict(row)) for row in rows]

    @staticmethod
    def get(report_id: str) -> Report | None:
        with session() as conn:
            row = conn.execute(
                text(f"SELECT {REPORT_COLUMNS} FROM reports WHERE id = :id"),
                {"id": report_id},
            ).mappings().first()
        return Report(**dict(row)) if row else None

    @staticmethod
    def get_raw(report_id: str) -> str | None:
        with session() as conn:
            row = conn.execute(
                text("SELECT raw_json FROM reports WHERE id = :id"),
                {"id": report_id},
            ).mappings().first()
        return row["raw_json"] if row else None

    @staticmethod
    def get_pdf(report_id: str) -> bytes | None:
        with session() as conn:
            row = conn.execute(
                text("SELECT pdf_bytes FROM reports WHERE id = :id"),
                {"id": report_id},
            ).mappings().first()
        return bytes(row["pdf_bytes"]) if row and row["pdf_bytes"] else None

    @staticmethod
    def count_datapoints(report_id: str) -> int:
        with session() as conn:
            return int(
                conn.execute(
                    text("SELECT COUNT(*) FROM report_datapoints WHERE report_id = :id"),
                    {"id": report_id},
                ).scalar_one()
            )

    @staticmethod
    def create_datapoints(datapoints: list[Datapoint]) -> None:
        if not datapoints:
            return

        with session() as conn:
            conn.execute(
                text(
                    f"""
                    INSERT INTO report_datapoints ({DATAPOINT_COLUMNS})
                    VALUES (
                        :id, :report_id, :report_code, :report_title, :row_code,
                        :column_code, :attribute_name, :value, :unit, :currency,
                        :identifiers_json
                    )
                    """
                ),
                [
                    {
                        "id": item.id,
                        "report_id": item.report_id,
                        "report_code": item.report_code,
                        "report_title": item.report_title,
                        "row_code": item.row_code,
                        "column_code": item.column_code,
                        "attribute_name": item.attribute_name,
                        "value": item.value,
                        "unit": item.unit,
                        "currency": item.currency,
                        "identifiers_json": (
                            json.dumps(item.identifiers) if item.identifiers else None
                        ),
                    }
                    for item in datapoints
                ],
            )

    @staticmethod
    def _datapoint(row) -> Datapoint:
        values = dict(row)
        identifiers = _identifiers_from_json(values.pop("identifiers_json", None))
        return Datapoint(**values, identifiers=identifiers)

    @staticmethod
    def get_datapoints(report_id: str) -> list[Datapoint]:
        with session() as conn:
            rows = conn.execute(
                text(
                    f"""
                    SELECT {DATAPOINT_COLUMNS} FROM report_datapoints
                    WHERE report_id = :report_id ORDER BY row_code, column_code
                    """
                ),
                {"report_id": report_id},
            ).mappings().all()
        return [ReportRepository._datapoint(row) for row in rows]

    @staticmethod
    def get_datapoint(datapoint_id: str) -> Datapoint | None:
        with session() as conn:
            row = conn.execute(
                text(
                    f"SELECT {DATAPOINT_COLUMNS} FROM report_datapoints WHERE id = :id"
                ),
                {"id": datapoint_id},
            ).mappings().first()
        return ReportRepository._datapoint(row) if row else None