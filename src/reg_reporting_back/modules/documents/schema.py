from typing import Any

from pydantic import BaseModel, Field

TableRows = list[list[str | None]]


class ParsedPage(BaseModel):
    page: int = Field(description="1-based page number")
    text: str


class ParsedTable(BaseModel):
    page: int = Field(description="1-based page number the table was found on")
    rows: TableRows | None = Field(
        default=None,
        description="Raw cells as extracted from the page",
    )
    headers: list[str] | None = Field(
        default=None,
        description="First row of the table, present when as_records is used",
    )
    records: list[dict[str, Any]] | None = Field(
        default=None,
        description="Rows keyed by header, present when as_records is used",
    )


class ParsedDocument(BaseModel):
    file_name: str | None = None
    page_count: int = 0
    table_count: int = 0
    pages: list[ParsedPage] = Field(default_factory=list)
    tables: list[ParsedTable] = Field(default_factory=list)


class ErrorResponse(BaseModel):
    detail: str
