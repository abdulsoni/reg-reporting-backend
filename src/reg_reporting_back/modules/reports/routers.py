"""Report endpoints: the regulatory returns and the figures inside them."""

from fastapi import APIRouter
from fastapi.responses import Response

from ...core.routing import run_operation
from .schema import (
    Datapoint,
    GenerateReportRequest,
    GenerateReportResult,
    ReportDetail,
    ReportSummary,
)
from .service import ReportService

router = APIRouter(prefix="/reports", tags=["Reports"])


@router.get("", response_model=list[ReportSummary])
def list_reports() -> list[ReportSummary]:
    """List the stored reports, newest first."""

    return run_operation(ReportService.list_reports)


@router.post("/generate-pdf", response_model=GenerateReportResult)
def generate_pdf(payload: GenerateReportRequest) -> GenerateReportResult:
    """Generate a regulatory return PDF, store it and read back its datapoints."""

    return run_operation(lambda: ReportService.generate(payload))


@router.get("/{report_id}", response_model=ReportDetail)
def get_report(report_id: str) -> ReportDetail:
    """Return a stored report with the raw parse that produced it."""

    return run_operation(lambda: ReportService.get_report(report_id))


@router.get("/{report_id}/datapoints", response_model=list[Datapoint])
def get_datapoints(report_id: str) -> list[Datapoint]:
    """Return the datapoints of a report, for the datapoint picker."""

    return run_operation(lambda: ReportService.get_datapoints(report_id))


@router.get(
    "/{report_id}/pdf",
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}}}},
)
def get_pdf(report_id: str) -> Response:
    """Download the stored PDF of a report."""

    return run_operation(
        lambda: Response(
            content=ReportService.get_pdf(report_id),
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'attachment; filename="{report_id}.pdf"',
            },
        )
    )