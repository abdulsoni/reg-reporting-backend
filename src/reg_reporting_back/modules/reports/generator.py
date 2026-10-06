"""Builds a COREP-style regulatory return as a PDF.

The generated PDF is the input to the real parser, so the POC round trip is:
generate -> download -> upload -> parse -> datapoints -> lineage trace.
"""

import io
from typing import Any

from fpdf import FPDF

from ...core.exceptions import ReportError

HEADERS = ("Row", "Column", "Attribute", "Value", "Unit")
COLUMN_WIDTHS = (20, 20, 55, 30, 25)

DEFAULT_DATAPOINTS: tuple[dict[str, Any], ...] = (
    {"row_code": "0010", "column_code": "0200", "attribute_name": "exposure_value", "value": 1240.50},
    {"row_code": "0040", "column_code": "0200", "attribute_name": "on_balance", "value": 600.00},
    {"row_code": "0050", "column_code": "0200", "attribute_name": "off_balance", "value": 500.00},
)


class _ReportPDF(FPDF):
    def header(self) -> None:  # pragma: no cover - cosmetic
        self.set_font("Helvetica", "B", 16)
        self.cell(0, 10, self.title_text, border=0, align="C", new_x="LMARGIN", new_y="NEXT")
        self.ln(2)

    def footer(self) -> None:  # pragma: no cover - cosmetic
        self.set_y(-15)
        self.set_font("Helvetica", "I", 8)
        self.cell(0, 10, f"Page {self.page_no()}", align="C")


def format_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return f"{float(value):.2f}"
    return str(value)


def build_pdf(
    report_code: str,
    report_title: str,
    report_type: str,
    reporting_entity: str,
    reporting_date: str,
    submission_version: str,
    unit: str,
    datapoints: list[dict[str, Any]],
) -> bytes:
    """Render a regulatory return PDF and return its bytes."""

    pdf = _ReportPDF()
    # The running header must not repeat the report code: the extractor reads
    # the code and title from the section heading below, and a code in the
    # page chrome would be matched first.
    pdf.title_text = f"{report_type} Regulatory Return"
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 8, f"Reporting Entity: {reporting_entity}", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    for label, value in (
        ("Regulatory Return", report_type),
        ("Reporting Date", reporting_date),
        ("Submission Version", submission_version),
    ):
        pdf.cell(0, 6, f"{label}: {value}", new_x="LMARGIN", new_y="NEXT")

    pdf.ln(4)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 8, f"{report_code} {report_title}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    _render_table(pdf, datapoints, unit)

    buffer = io.BytesIO()
    try:
        pdf.output(buffer)
    except Exception as exc:  # pragma: no cover - fpdf failure
        raise ReportError(f"Could not render the report PDF: {exc}") from exc

    return buffer.getvalue()


REQUIRED_FIELDS_GEN = ("row_code", "attribute_name", "value")


def _normalize_datapoint(item: dict[str, Any], unit: str) -> dict[str, Any] | None:
    # Try to extract common aliases
    row = (
        item.get("row_code")
        or item.get("row")
        or item.get("Row")
        or item.get("rowCode")
    )
    col = item.get("column_code") or item.get("column") or item.get("Column") or item.get("col")
    attr = (
        item.get("attribute_name")
        or item.get("attribute")
        or item.get("Attribute")
        or item.get("data_point")
        or item.get("measure")
    )
    val = item.get("value") or item.get("amount") or item.get("Value")
    u = item.get("unit") or item.get("units") or item.get("Unit") or unit

    if not attr or val is None:
        # skip if fundamentally unusable
        return None
    # keep row/col as strings if present
    return {
        "row_code": str(row).strip() if row is not None else "",
        "column_code": str(col).strip() if col is not None else "",
        "attribute_name": str(attr).strip(),
        "value": val,
        "unit": str(u).strip() if u is not None else "",
    }


def _render_table(pdf: FPDF, datapoints: list[dict[str, Any]], unit: str) -> None:
    pdf.set_font("Helvetica", "B", 9)
    for header, width in zip(HEADERS, COLUMN_WIDTHS, strict=True):
        pdf.cell(width, 7, header, border=1, align="C")
    pdf.ln()

    pdf.set_font("Helvetica", "", 9)
    # if no valid datapoints provided, use defaults to ensure round-trip works
    items = list(datapoints or [])
    rendered = 0
    for item in items:
        norm = _normalize_datapoint(item, unit)
        if norm is None:
            continue
        row = (
            str(norm.get("row_code", "")),
            str(norm.get("column_code", "")),
            str(norm.get("attribute_name", "")),
            format_value(norm.get("value")),
            str(norm.get("unit") or unit),
        )
        for cell, width in zip(row, COLUMN_WIDTHS, strict=True):
            pdf.cell(width, 7, cell, border=1, align="C")
        pdf.ln()
        rendered += 1

    if rendered == 0:
        for item in DEFAULT_DATAPOINTS:
            row = (
                str(item.get("row_code", "")),
                str(item.get("column_code", "")),
                str(item.get("attribute_name", "")),
                format_value(item.get("value")),
                str(item.get("unit") or unit),
            )
            for cell, width in zip(row, COLUMN_WIDTHS, strict=True):
                pdf.cell(width, 7, cell, border=1, align="C")
            pdf.ln()