"""Report service: generating, storing and serving regulatory returns."""

import json
from typing import Any

from ...core.exceptions import ReportError, ReportNotFound
from ..documents.schema import ParsedDocument
from ..lineage.schema import new_id, utc_now
from .extractor import extract_datapoints, normalize_date, read_header, require_datapoints
from .generator import DEFAULT_DATAPOINTS, build_pdf
from .repository import Datapoint, Report, ReportRepository
from .schema import (
    Datapoint as DatapointModel,
    GenerateReportRequest,
    GenerateReportResult,
    ReportDetail,
    ReportSummary,
)


def _summary(report: Report) -> ReportSummary:
    return ReportSummary(
        id=report.id,
        file_name=report.file_name,
        report_type=report.report_type,
        reporting_entity=report.reporting_entity,
        reporting_date=report.reporting_date,
        submission_version=report.submission_version,
        page_count=report.page_count,
        table_count=report.table_count,
        datapoint_count=ReportRepository.count_datapoints(report.id),
        created_at=report.created_at,
    )


def _to_model(datapoint: Datapoint) -> DatapointModel:
    return DatapointModel(
        id=datapoint.id,
        report_id=datapoint.report_id,
        report_code=datapoint.report_code,
        report_title=datapoint.report_title,
        row_code=datapoint.row_code,
        column_code=datapoint.column_code,
        attribute_name=datapoint.attribute_name,
        value=datapoint.value,
        unit=datapoint.unit,
        currency=datapoint.currency,
    )


def _require_report(report_id: str) -> Report:
    report = ReportRepository.get(report_id)
    if report is None:
        raise ReportNotFound(f"Report '{report_id}' was not found.")
    return report


class ReportService:
    """Owns the lifecycle of a report: generate, parse, store, serve."""

    @staticmethod
    def list_reports() -> list[ReportSummary]:
        return [_summary(report) for report in ReportRepository.list_all()]

    @staticmethod
    def get_report(report_id: str) -> ReportDetail:
        report = _require_report(report_id)
        raw_json = ReportRepository.get_raw(report_id)
        raw = ParsedDocument.model_validate(json.loads(raw_json)) if raw_json else None

        detail = _summary(report)
        return ReportDetail(**detail.model_dump(), raw=raw)

    @staticmethod
    def get_datapoints(report_id: str) -> list[DatapointModel]:
        _require_report(report_id)
        return [_to_model(item) for item in ReportRepository.get_datapoints(report_id)]

    @staticmethod
    def get_pdf(report_id: str) -> bytes:
        _require_report(report_id)
        pdf_bytes = ReportRepository.get_pdf(report_id)
        if pdf_bytes is None:
            raise ReportError(f"Report '{report_id}' has no stored PDF.")
        return pdf_bytes

    @staticmethod
    def generate(payload: GenerateReportRequest) -> GenerateReportResult:
        """Render a regulatory return, store it and read its datapoints back.

        The PDF is parsed with the same code path an upload uses, so the stored
        datapoints are exactly what the parser produces.
        """

        rows: list[dict[str, Any]] = list(payload.datapoints or DEFAULT_DATAPOINTS)
        reporting_date = normalize_date(payload.reporting_date) or payload.reporting_date

        pdf_bytes = build_pdf(
            report_code=payload.report_code,
            report_title=payload.report_title,
            report_type=payload.report_type,
            reporting_entity=payload.reporting_entity,
            reporting_date=reporting_date,
            submission_version=payload.submission_version,
            unit=payload.unit,
            datapoints=rows,
        )

        from ..documents.service import DocumentService  # local import avoids a cycle

        parsed = DocumentService.parse_pdf(pdf_bytes, file_name=f"{payload.report_code}.pdf")

        report = _persist(
            file_name=f"corep_{payload.report_code.split('.')[0].lower()}.pdf",
            parsed=parsed,
            pdf_bytes=pdf_bytes,
            currency=payload.currency,
        )

        return GenerateReportResult(
            report=_summary(report),
            datapoints=[_to_model(item) for item in ReportRepository.get_datapoints(report.id)],
        )

    @staticmethod
    def store_upload(
        file_name: str,
        parsed: ParsedDocument,
        pdf_bytes: bytes,
    ) -> ReportSummary:
        """Persist an uploaded report and the datapoints parsed out of it."""

        report = _persist(
            file_name=file_name,
            parsed=parsed,
            pdf_bytes=pdf_bytes,
            currency=None,
        )
        return _summary(report)


def _persist(
    file_name: str,
    parsed: ParsedDocument,
    pdf_bytes: bytes,
    currency: str | None,
) -> Report:
    header = read_header(parsed)
    report = Report(
        id=new_id("report"),
        file_name=file_name,
        report_type=header.report_type,
        reporting_entity=header.reporting_entity,
        reporting_date=header.reporting_date,
        submission_version=header.submission_version,
        page_count=parsed.page_count,
        table_count=parsed.table_count,
        created_at=utc_now(),
    )

    ReportRepository.create(report, parsed.model_dump_json(), pdf_bytes)

    datapoints = extract_datapoints(parsed, header, report.id, currency, new_id)
    require_datapoints(datapoints, report.id)
    ReportRepository.create_datapoints(
        [
            Datapoint(
                id=item.id,
                report_id=item.report_id,
                report_code=item.report_code,
                report_title=item.report_title,
                row_code=item.row_code,
                column_code=item.column_code,
                attribute_name=item.attribute_name,
                value=item.value,
                unit=item.unit,
                currency=item.currency,
            )
            for item in datapoints
        ]
    )

    return report