"""Reports module: regulatory returns, their datapoints and the demo PDF."""

from .extractor import ReportHeader, extract_datapoints, normalize_date, read_header
from .generator import build_pdf
from .repository import Datapoint, Report, ReportRepository
from .schema import (
    Datapoint as DatapointModel,
    GenerateReportRequest,
    GenerateReportResult,
    ReportDetail,
    ReportSummary,
)
from .service import ReportService

__all__ = [
    "Datapoint",
    "DatapointModel",
    "GenerateReportRequest",
    "GenerateReportResult",
    "Report",
    "ReportDetail",
    "ReportHeader",
    "ReportRepository",
    "ReportService",
    "ReportSummary",
    "build_pdf",
    "extract_datapoints",
    "normalize_date",
    "read_header",
]