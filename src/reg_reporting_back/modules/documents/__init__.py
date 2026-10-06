"""Documents module: reading PDFs and extracting their text and tables."""

from .schema import ErrorResponse, ParsedDocument, ParsedPage, ParsedTable
from .service import DocumentService

__all__ = [
    "DocumentService",
    "ErrorResponse",
    "ParsedDocument",
    "ParsedPage",
    "ParsedTable",
]