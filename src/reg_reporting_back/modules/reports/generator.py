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

NORMALIZED_HEADERS = ("Normalized ID", "Trade ID", "Sensitivity Type", "Normalized USD")
NORMALIZED_WIDTHS = (45, 35, 40, 40)

DEFAULT_DATAPOINTS: tuple[dict[str, Any], ...] = (
    {"row_code": "0010", "column_code": "0200", "attribute_name": "exposure_value", "value": 1240.50},
    {"row_code": "0040", "column_code": "0200", "attribute_name": "on_balance", "value": 600.00},
    {"row_code": "0050", "column_code": "0200", "attribute_name": "off_balance", "value": 500.00},
)

# Scenario B: the normalized sensitivity sheet. The full sheet is rendered,
# one row per normalized sensitivity, and each row carries its identifying
# columns so a trace can locate the exact source record. NORM-SENS-00005 keeps
# the value supplied on the sheet, even though the declared formula produces a
# different figure; the mismatch is surfaced by the reconciliation check.
DEFAULT_NORMALIZED_DATAPOINTS: tuple[dict[str, Any], ...] = (
    {
        "identifiers": {
            "normalized_id": "NORM-SENS-00001",
            "trade_id": "TRD-0001",
            "sensitivity_type": "DELTA",
        },
        "value": 405.72,
    },
    {
        "identifiers": {
            "normalized_id": "NORM-SENS-00002",
            "trade_id": "TRD-0002",
            "sensitivity_type": "DELTA",
        },
        "value": 561.99,
    },
    {
        "identifiers": {
            "normalized_id": "NORM-SENS-00003",
            "trade_id": "TRD-0002",
            "sensitivity_type": "VEGA",
        },
        "value": 37.47,
    },
    {
        "identifiers": {
            "normalized_id": "NORM-SENS-00004",
            "trade_id": "TRD-0002",
            "sensitivity_type": "CURVATURE",
        },
        "value": 7.49,
    },
    {
        "identifiers": {
            "normalized_id": "NORM-SENS-00005",
            "trade_id": "TRD-0003",
            "sensitivity_type": "DELTA",
        },
        "value": 3.18,
    },
    {
        "identifiers": {
            "normalized_id": "NORM-SENS-00006",
            "trade_id": "TRD-0003",
            "sensitivity_type": "VEGA",
        },
        "value": 0.23,
    },
    {
        "identifiers": {
            "normalized_id": "NORM-SENS-00007",
            "trade_id": "TRD-0003",
            "sensitivity_type": "CURVATURE",
        },
        "value": 0.05,
    },
    {
        "identifiers": {
            "normalized_id": "NORM-SENS-00008",
            "trade_id": "TRD-0004",
            "sensitivity_type": "DELTA",
        },
        "value": 720.00,
    },
    {
        "identifiers": {
            "normalized_id": "NORM-SENS-00009",
            "trade_id": "TRD-0005",
            "sensitivity_type": "DELTA",
        },
        "value": 656.25,
    },
    {
        "identifiers": {
            "normalized_id": "NORM-SENS-00010",
            "trade_id": "TRD-0006",
            "sensitivity_type": "DELTA",
        },
        "value": 829.92,
    },
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
    sheet: str = "corep",
    source_dataset: str | None = None,
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

    if source_dataset:
        pdf.cell(0, 6, f"Source Dataset: {source_dataset}", new_x="LMARGIN", new_y="NEXT")

    pdf.ln(4)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 8, f"{report_code} {report_title}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    if sheet == "normalized":
        _render_table(
            pdf,
            NORMALIZED_HEADERS,
            NORMALIZED_WIDTHS,
            _normalized_rows(datapoints),
        )
    else:
        _render_table(
            pdf,
            HEADERS,
            COLUMN_WIDTHS,
            _corep_rows(datapoints, unit),
        )

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


def _corep_rows(datapoints: list[dict[str, Any]], unit: str) -> list[tuple[str, ...]]:
    """Render datapoint dicts as Row/Column/Attribute/Value/Unit cells."""

    rows: list[tuple[str, ...]] = []
    for item in list(datapoints or []):
        norm = _normalize_datapoint(item, unit)
        if norm is None:
            continue
        rows.append(
            (
                str(norm.get("row_code", "")),
                str(norm.get("column_code", "")),
                str(norm.get("attribute_name", "")),
                format_value(norm.get("value")),
                str(norm.get("unit") or unit),
            )
        )

    # If no valid datapoints were provided, fall back to the seeded figures so
    # the generate -> download -> upload round trip always works.
    if not rows:
        rows = [
            (
                str(item.get("row_code", "")),
                str(item.get("column_code", "")),
                str(item.get("attribute_name", "")),
                format_value(item.get("value")),
                str(item.get("unit") or unit),
            )
            for item in DEFAULT_DATAPOINTS
        ]

    return rows


def _normalized_rows(datapoints: list[dict[str, Any]]) -> list[tuple[str, ...]]:
    """Render normalized sheet rows as Normalized ID/Trade/Sensitivity/Value."""

    rows: list[tuple[str, ...]] = []
    for item in list(datapoints or []):
        identifiers = {
            str(key): ("" if value is None else str(value))
            for key, value in (item.get("identifiers") or {}).items()
        }
        value = item.get("value", item.get("normalized_usd"))
        normalized_id = (
            identifiers.get("normalized_id")
            or (str(item.get("normalized_id") or "") or None)
            or (str(item.get("row_code") or "") or None)
        )
        if not normalized_id or value is None:
            continue
        rows.append(
            (
                normalized_id,
                identifiers.get("trade_id") or str(item.get("trade_id") or ""),
                identifiers.get("sensitivity_type") or str(item.get("sensitivity_type") or ""),
                format_value(value),
            )
        )

    if not rows:
        rows = [
            (
                item["identifiers"]["normalized_id"],
                item["identifiers"]["trade_id"],
                item["identifiers"]["sensitivity_type"],
                format_value(item["value"]),
            )
            for item in DEFAULT_NORMALIZED_DATAPOINTS
        ]

    return rows


def _render_table(
    pdf: FPDF,
    headers: tuple[str, ...],
    widths: tuple[int, ...],
    rows: list[tuple[str, ...]],
) -> None:
    pdf.set_font("Helvetica", "B", 9)
    for header, width in zip(headers, widths, strict=True):
        pdf.cell(width, 7, header, border=1, align="C")
    pdf.ln()

    pdf.set_font("Helvetica", "", 9)
    for row in rows:
        for cell, width in zip(row, widths, strict=True):
            pdf.cell(width, 7, str(cell), border=1, align="C")
        pdf.ln()