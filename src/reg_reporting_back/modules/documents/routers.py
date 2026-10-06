"""Document endpoints: upload a regulatory return and get it parsed."""

from fastapi import APIRouter, File, HTTPException, UploadFile

from ...core.config import settings
from ...core.routing import run_operation
from ..reports.schema import UploadedDocument
from ..reports.service import ReportService
from .schema import ParsedDocument
from .service import DocumentService

router = APIRouter(
    prefix="/documents",
    tags=["Documents"],
)

CHUNK_SIZE = 1024 * 1024
ALLOWED_CONTENT_TYPES = {
    "application/pdf",
    "application/x-pdf",
    "application/octet-stream",
    "",
}


async def _read_upload(file: UploadFile) -> bytes:
    """Read an upload into memory, refusing anything over the configured cap."""

    limit = settings.max_upload_size_bytes
    buffer = bytearray()

    while chunk := await file.read(CHUNK_SIZE):
        buffer.extend(chunk)
        if len(buffer) > limit:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"File is larger than the {settings.MAX_UPLOAD_SIZE_MB} MB limit."
                ),
            )

    return bytes(buffer)


@router.post(
    "/upload-pdf",
    response_model=UploadedDocument,
    responses={
        400: {"description": "Invalid PDF upload"},
        413: {"description": "File too large"},
    },
)
async def upload_pdf(file: UploadFile = File(...)) -> UploadedDocument:
    """Upload a regulatory return, parse it and store it as a report.

    The parsed datapoints are what the lineage tracer starts from, so they are
    returned alongside the report id the UI needs.
    """

    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(
            status_code=400,
            detail="Only PDF files are supported",
        )

    if (file.content_type or "").lower() not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unsupported content type '{file.content_type}'. "
                "Only PDF files are supported"
            ),
        )

    file_bytes = await _read_upload(file)

    try:
        parsed = run_operation(
            lambda: DocumentService.parse_pdf(file_bytes, file_name=file.filename)
        )
        report = run_operation(
            lambda: ReportService.store_upload(file.filename, parsed, file_bytes)
        )
    finally:
        await file.close()

    return UploadedDocument(
        report_id=report.id,
        report=report,
        document=parsed,
        # The datapoint ids are what the lineage tracer starts from, so the UI
        # needs them in the same response rather than a second round trip.
        datapoints=ReportService.get_datapoints(report.id),
    )


@router.post(
    "/parse-pdf",
    response_model=ParsedDocument,
    responses={400: {"description": "Invalid PDF upload"}, 413: {"description": "File too large"}},
)
async def parse_pdf(file: UploadFile = File(...)) -> ParsedDocument:
    """Parse an uploaded PDF without storing it as a report."""

    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported")

    if (file.content_type or "").lower() not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unsupported content type '{file.content_type}'. "
                "Only PDF files are supported"
            ),
        )

    file_bytes = await _read_upload(file)

    try:
        return run_operation(
            lambda: DocumentService.parse_pdf(file_bytes, file_name=file.filename)
        )
    finally:
        await file.close()