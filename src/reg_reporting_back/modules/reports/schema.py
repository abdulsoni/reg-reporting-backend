"""Request and response models for reports and their datapoints."""

from typing import Any

from pydantic import BaseModel, Field

from ..documents.schema import ParsedDocument


class Datapoint(BaseModel):
    """One reported figure, identified by its report/row/column coordinates."""

    id: str
    report_id: str
    report_code: str | None = None
    report_title: str | None = None
    row_code: str | None = None
    column_code: str | None = None
    attribute_name: str | None = None
    value: float | None = None
    unit: str | None = None
    currency: str | None = None


class ReportSummary(BaseModel):
    """A report as it appears in the report picker."""

    id: str
    file_name: str
    report_type: str | None = None
    reporting_entity: str | None = None
    reporting_date: str | None = None
    submission_version: str | None = None
    page_count: int = 0
    table_count: int = 0
    datapoint_count: int = 0
    created_at: str


class ReportDetail(ReportSummary):
    """A report plus the raw parse that produced it."""

    raw: ParsedDocument | None = None


class GenerateReportRequest(BaseModel):
    """Body for building a demo regulatory return."""

    report_type: str = "COREP"
    reporting_entity: str = "J P Morgan Bank"
    reporting_date: str = "2026-06-30"
    submission_version: str = "v1"
    report_code: str = "C07.00"
    report_title: str = "Credit Risk SA - Corporates"
    unit: str = "EUR m"
    currency: str = "EUR"
    datapoints: list[dict[str, Any]] | None = Field(
        default=None,
        description="Rows to render; defaults to the seeded COREP figures",
    )


class GenerateReportResult(BaseModel):
    status: str = "success"
    report: ReportSummary
    datapoints: list[Datapoint]


class UploadedDocument(BaseModel):
    """The result of an upload: the parse, plus the report it was stored as."""

    report_id: str
    report: ReportSummary
    document: ParsedDocument
    datapoints: list[Datapoint] = Field(
        default_factory=list,
        description="The figures extracted from the report, with their ids",
    )