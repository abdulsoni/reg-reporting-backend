import io
import re
from typing import Any

import pdfplumber

from ...core.exceptions import PdfParseError
from .schema import ParsedDocument, ParsedPage, ParsedTable, TableRows

PDF_MAGIC = b"%PDF-"
WHITESPACE = re.compile(r"\s+")


class DocumentService:

    @staticmethod
    def parse_pdf(
        file_bytes: bytes,
        as_records: bool = False,
        file_name: str | None = None,
    ) -> ParsedDocument:
        """Extract the text and tables of a PDF document.

        Args:
            file_bytes: Raw content of the PDF file.
            as_records: When True, tables are returned as headers plus records
                keyed by header instead of raw cell rows.
            file_name: Optional name echoed back in the response.
        """

        DocumentService._validate(file_bytes)

        result = ParsedDocument(file_name=file_name)

        try:
            with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                result.page_count = len(pdf.pages)

                for page_number, page in enumerate(pdf.pages, start=1):
                    result.pages.append(
                        ParsedPage(
                            page=page_number,
                            text=page.extract_text() or "",
                        )
                    )

                    for table in page.extract_tables():
                        if not table:
                            continue

                        result.tables.append(
                            DocumentService._to_parsed_table(
                                page_number=page_number,
                                table=table,
                                as_records=as_records,
                            )
                        )
        except PdfParseError:
            raise
        except Exception as exc:
            raise PdfParseError(f"Could not read the PDF file: {exc}") from exc

        result.table_count = len(result.tables)
        return result

    @staticmethod
    def _validate(file_bytes: bytes) -> None:
        if not file_bytes:
            raise PdfParseError("The uploaded file is empty.")

        if not file_bytes.lstrip()[:5].startswith(PDF_MAGIC):
            raise PdfParseError(
                "The uploaded file is not a valid PDF (missing %PDF- header)."
            )

    @staticmethod
    def _to_parsed_table(
        page_number: int,
        table: list[list[str | None]],
        as_records: bool,
    ) -> ParsedTable:
        rows: TableRows = [
            [cell if cell is None else DocumentService._clean_cell(cell) for cell in row]
            for row in table
        ]

        if not as_records:
            return ParsedTable(page=page_number, rows=rows)

        headers, records = DocumentService._build_records(rows)
        return ParsedTable(
            page=page_number,
            headers=headers,
            records=records,
        )

    @staticmethod
    def _build_records(
        rows: TableRows,
    ) -> tuple[list[str], list[dict[str, Any]]]:
        if not rows:
            return [], []

        headers = DocumentService._normalize_headers(rows[0])
        records = [
            {
                header: value
                for header, value in zip(headers, row, strict=False)
            }
            for row in rows[1:]
        ]
        return headers, records

    @staticmethod
    def _normalize_headers(header_row: TableRows[0]) -> list[str]:
        headers: list[str] = []
        seen: set[str] = set()

        for index, cell in enumerate(header_row, start=1):
            name = (cell or "").strip() or f"column_{index}"

            if name in seen:
                name = f"{name}_{index}"
                while name in seen:
                    name = f"{name}_"

            seen.add(name)
            headers.append(name)

        return headers

    @staticmethod
    def _clean_cell(value: str) -> str:
        return WHITESPACE.sub(" ", value).strip()
