"""Extracts datapoints and report metadata from a parsed regulatory return.

A parsed table is only turned into datapoints when its header row names the
coordinates we need, so an unrelated table on the same page is ignored rather
than producing nonsense coordinates.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from ...core.exceptions import ReportError
from ..documents.schema import ParsedDocument
from .schema import Datapoint

# e.g. "C07.00 Credit Risk SA - Corporates"
REPORT_CODE_PATTERN = re.compile(r"([A-Z]\d{2}\.\d{2})\b")

# Header aliases, so "Row code" and "row" are both understood.
HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "row_code": ("row", "row_code", "row_code_"),
    "column_code": ("column", "column_code", "col", "col_code"),
    "attribute_name": ("attribute", "attribute_name", "data_point", "measure"),
    "value": ("value", "amount", "reported_value", "reported_value_usd"),
    "unit": ("unit", "units", "currency"),
}

REQUIRED_FIELDS = frozenset({"row_code", "attribute_name", "value"})

DATE_LABEL_PATTERN = re.compile(r"reporting date[ \t]*:?[ \t]*(.+)", re.IGNORECASE)
ENTITY_LABEL_PATTERN = re.compile(r"reporting entity[ \t]*:?[ \t]*(.+)", re.IGNORECASE)
VERSION_LABEL_PATTERN = re.compile(r"submission version[ \t]*:?[ \t]*(.+)", re.IGNORECASE)
RETURN_LABEL_PATTERN = re.compile(r"regulatory return[ \t]*:?[ \t]*(.+)", re.IGNORECASE)

MONTHS = {
    name: index
    for index, name in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun",
         "jul", "aug", "sep", "oct", "nov", "dec"),
        start=1,
    )
}

VALUE_PATTERN = re.compile(r"-?\d+(\.\d+)?")


@dataclass(frozen=True, slots=True)
class ReportHeader:
    """Metadata read from the pages of a regulatory return."""

    report_code: str | None = None
    report_title: str | None = None
    report_type: str | None = None
    reporting_entity: str | None = None
    reporting_date: str | None = None
    submission_version: str | None = None


def normalize_date(value: str | None) -> str | None:
    """Accept both ISO dates and the '30 Jun 2026' form used in reports."""

    if not value:
        return None

    cleaned = value.strip().rstrip(".")
    try:
        return date.fromisoformat(cleaned).isoformat()
    except ValueError:
        pass

    match = re.match(r"(\d{1,2})\s+([A-Za-z]{3})[a-z]*\s+(\d{4})", cleaned)
    if match and match.group(2).lower() in MONTHS:
        day, month, year = match.groups()
        return f"{year}-{MONTHS[month.lower()]:02d}-{int(day):02d}"

    return None


def _labelled(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    if not match:
        return None
    value = match.group(1).splitlines()[0].strip().rstrip(".")
    return value or None


def read_header(document: ParsedDocument) -> ReportHeader:
    """Read report-level metadata out of the page text."""

    text = "\n".join(page.text for page in document.pages)

    report_code = None
    report_title = None
    code_match = REPORT_CODE_PATTERN.search(text)
    if code_match:
        report_code = code_match.group(1)
        # Index within the line, not the whole document text.
        line = text[code_match.start() :].splitlines()[0]
        report_title = line[code_match.end() - code_match.start() :].strip() or None

    return ReportHeader(
        report_code=report_code,
        report_title=report_title,
        report_type=_labelled(RETURN_LABEL_PATTERN, text),
        reporting_entity=_labelled(ENTITY_LABEL_PATTERN, text),
        reporting_date=normalize_date(_labelled(DATE_LABEL_PATTERN, text)),
        submission_version=_labelled(VERSION_LABEL_PATTERN, text),
    )


def _normalize_header(cell: str | None) -> str:
    return (cell or "").strip().lower().replace(" ", "_")


def _column_map(header_row: list[str | None]) -> dict[str, int]:
    """Map a header row onto our field names, or return an empty map."""

    normalized = [_normalize_header(cell) for cell in header_row]
    mapping: dict[str, int] = {}

    for field, aliases in HEADER_ALIASES.items():
        for index, name in enumerate(normalized):
            if name in aliases:
                mapping[field] = index
                break

    return mapping if REQUIRED_FIELDS <= mapping.keys() else {}


def parse_number(cell: str | None) -> float | None:
    if cell is None:
        return None
    value = cell.strip().replace(",", "").replace("\u00a0", "")
    if not VALUE_PATTERN.fullmatch(value):
        return None
    return float(value)


def table_rows(table) -> list[dict[str, str | float | None]]:
    """Turn one parsed table into datapoint rows, if its header allows it."""

    if not table.rows:
        return []

    mapping = _column_map(table.rows[0])
    if not mapping:
        return []

    def cell(row: list[str | None], index: int) -> str | None:
        return row[index] if index < len(row) else None

    rows: list[dict[str, str | float | None]] = []
    for row in table.rows[1:]:
        attribute = (cell(row, mapping["attribute_name"]) or "").strip()
        if not attribute:
            continue
        unit = cell(row, mapping["unit"]) if "unit" in mapping else None
        rows.append(
            {
                "row_code": (cell(row, mapping["row_code"]) or "").strip() or None,
                "column_code": (cell(row, mapping["column_code"]) or "").strip() or None,
                "attribute_name": attribute,
                "value": parse_number(cell(row, mapping["value"])),
                "unit": (unit or "").strip() or None,
            }
        )

    return rows


CURRENCY_UNIT_PATTERN = re.compile(r"^([A-Z]{3})\b")


def infer_currency(unit: str | None) -> str | None:
    """Read the currency out of a unit such as 'EUR m' or 'USD thousands'."""

    match = CURRENCY_UNIT_PATTERN.match((unit or "").strip())
    return match.group(1) if match else None


def extract_datapoints(
    document: ParsedDocument,
    header: ReportHeader,
    report_id: str,
    currency: str | None,
    new_id: Callable[[str], str],
) -> list[Datapoint]:
    """Turn every qualifying table in the document into datapoints."""

    rows = [row for table in document.tables for row in table_rows(table)]

    return [
        Datapoint(
            id=new_id("dp"),
            report_id=report_id,
            report_code=header.report_code,
            report_title=header.report_title,
            row_code=row["row_code"],
            column_code=row["column_code"],
            attribute_name=row["attribute_name"],
            value=row["value"],
            unit=row["unit"],
            currency=currency or infer_currency(row["unit"]),
        )
        for row in rows
    ]


def require_datapoints(datapoints: list[Datapoint], report_id: str) -> None:
    if not datapoints:
        raise ReportError(
            f"No datapoint table was found in report '{report_id}'. "
            "A datapoint table needs Row, Column, Attribute and Value columns."
        )