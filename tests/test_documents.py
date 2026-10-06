"""The PDF round trip: generate a return, download it, upload it, read it back."""

import pytest

from reg_reporting_back.modules.reports import generator
from reg_reporting_back.modules.reports.extractor import extract_datapoints, read_header

COREP_CODE = "C07.00"
COREP_TITLE = "Credit Risk SA - Corporates"


def build_reference_pdf() -> bytes:
    return generator.build_pdf(
        report_code=COREP_CODE,
        report_title=COREP_TITLE,
        report_type="COREP",
        reporting_entity="J P Morgan Bank",
        reporting_date="2026-06-30",
        submission_version="v1",
        unit="EUR m",
        datapoints=list(generator.DEFAULT_DATAPOINTS),
    )


def parse(data: bytes):
    from reg_reporting_back.modules.documents.service import DocumentService

    return DocumentService.parse_pdf(data)


def test_generated_pdf_carries_the_reconciled_values() -> None:
    header = read_header(parse(build_reference_pdf()))

    assert header.report_code == COREP_CODE
    assert header.report_title == COREP_TITLE
    assert header.report_type == "COREP"
    assert header.reporting_entity == "J P Morgan Bank"
    assert header.reporting_date == "2026-06-30"
    assert header.submission_version == "v1"


def test_datapoints_survive_generation_and_parsing() -> None:
    document = parse(build_reference_pdf())
    datapoints = extract_datapoints(
        document, read_header(document), "report-test", None, lambda prefix: f"{prefix}-test"
    )

    assert [(d.row_code, d.attribute_name, d.value) for d in datapoints] == [
        ("0010", "exposure_value", 1240.50),
        ("0040", "on_balance", 600.00),
        ("0050", "off_balance", 500.00),
    ]
    # exposure is measured after credit-risk mitigation: it is the drawn plus
    # undrawn balances less the collateral allocated against them.
    exposure, on_balance, off_balance = (d.value for d in datapoints)
    allocated_collateral = 140.50
    assert exposure == on_balance + off_balance + allocated_collateral
    assert {d.currency for d in datapoints} == {"EUR"}


def test_generated_pdf_uses_a_real_table_layout() -> None:
    document = parse(build_reference_pdf())

    assert len(document.tables) == 1
    table = document.tables[0]
    assert table.rows is not None
    assert table.rows[0] == ["Row", "Column", "Attribute", "Value", "Unit"]
    assert table.rows[1][:3] == ["0010", "0200", "exposure_value"]
    assert table.rows[1][3] == "1240.50"


def test_upload_stores_the_report_and_its_datapoints(client) -> None:
    response = client.post(
        "/documents/upload-pdf",
        files={"file": ("corep_c07.pdf", build_reference_pdf(), "application/pdf")},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["report"]["file_name"] == "corep_c07.pdf"
    assert body["report"]["datapoint_count"] == 3

    datapoints = client.get(f"/reports/{body['report_id']}/datapoints").json()
    assert len(datapoints) == 3
    assert {d["attribute_name"] for d in datapoints} == {
        "exposure_value",
        "on_balance",
        "off_balance",
    }


def test_upload_returns_the_datapoints_it_stored(client) -> None:
    """The UI selects a figure straight from the upload response, so the ids
    the lineage tracer needs have to travel with the report rather than needing
    a second request."""
    body = client.post(
        "/documents/upload-pdf",
        files={"file": ("corep_c07.pdf", build_reference_pdf(), "application/pdf")},
    ).json()

    returned = body["datapoints"]
    stored = client.get(f"/reports/{body['report_id']}/datapoints").json()

    assert len(returned) == len(stored) == 3
    assert [d["id"] for d in returned] == [d["id"] for d in stored]
    assert all(d["report_id"] == body["report_id"] for d in returned)
    assert returned[0]["id"].startswith("dp-")
    assert returned[0]["row_code"] == "0010"
    assert returned[0]["column_code"] == "0200"
    assert returned[0]["attribute_name"] == "exposure_value"
    assert returned[0]["value"] == 1240.50


def test_generate_download_and_reupload_is_stable(client) -> None:
    generated = client.post("/reports/generate-pdf", json={}).json()
    report_id = generated["report"]["id"]
    assert generated["status"] == "success"

    pdf = client.get(f"/reports/{report_id}/pdf")
    assert pdf.status_code == 200
    assert pdf.headers["content-type"] == "application/pdf"

    uploaded = client.post(
        "/documents/upload-pdf",
        files={"file": ("corep_c07.pdf", pdf.content, "application/pdf")},
    ).json()

    original = client.get(f"/reports/{report_id}/datapoints").json()
    reuploaded = client.get(f"/reports/{uploaded['report_id']}/datapoints").json()
    strip = lambda items: [  # noqa: E731
        {k: v for k, v in item.items() if k not in {"id", "report_id"}} for item in items
    ]
    assert strip(reuploaded) == strip(original)


@pytest.mark.parametrize("payload", [b"", b"not a pdf at all"])
def test_upload_rejects_a_file_that_is_not_a_report(client, payload: bytes) -> None:
    response = client.post(
        "/documents/upload-pdf",
        files={"file": ("broken.pdf", payload, "application/pdf")},
    )

    assert response.status_code == 400
    assert response.json()["detail"]


def test_a_pdf_without_a_usable_table_is_rejected(client) -> None:
    from fpdf import FPDF

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=12)
    pdf.cell(0, 10, "C07.00 Credit Risk SA - Corporates", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 10, "Nothing tabular here", new_x="LMARGIN", new_y="NEXT")

    response = client.post(
        "/documents/upload-pdf",
        files={"file": ("empty.pdf", bytes(pdf.output()), "application/pdf")},
    )

    assert response.status_code == 400
    assert "datapoint" in response.json()["detail"].lower()