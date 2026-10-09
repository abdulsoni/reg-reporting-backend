"""Reports are the entry point of a trace, so they have to be readable back."""

import pytest


@pytest.fixture
def report_id(client) -> str:
    return client.post("/reports/generate-pdf", json={}).json()["report"]["id"]


def test_generation_stores_a_report_with_its_datapoints(client) -> None:
    body = client.post("/reports/generate-pdf", json={}).json()

    summary = body["report"]
    assert summary["report_type"] == "COREP"
    assert summary["page_count"] == 1
    assert summary["table_count"] == 1
    assert summary["datapoint_count"] == 3
    assert len(body["datapoints"]) == 3


def test_generation_accepts_overrides(client) -> None:
    body = client.post(
        "/reports/generate-pdf",
        json={
            "report_code": "C08.00",
            "report_title": "Credit Risk SA - Retail",
            "reporting_entity": "Example Bank",
            "reporting_date": "30 Jun 2026",
            "datapoints": [
                {"row_code": "0010", "column_code": "0200", "attribute_name": "exposure_value", "value": 99.5}
            ],
        },
    ).json()

    assert body["report"]["reporting_entity"] == "Example Bank"
    assert body["report"]["reporting_date"] == "2026-06-30"
    assert [d["value"] for d in body["datapoints"]] == [99.5]
    assert body["datapoints"][0]["report_code"] == "C08.00"


def test_the_normalized_scenario_publishes_a_c90_sheet(client) -> None:
    body = client.post("/reports/generate-pdf", json={"scenario": "normalized"}).json()

    summary = body["report"]
    assert summary["report_type"] == "COREP"
    assert summary["report_code"] == "C90.00"
    assert summary["report_title"] == "Market Risk Sensitivities"
    assert summary["source_dataset"] == "Normalized Sensitivities"
    assert summary["currency"] == "USD"
    assert summary["reporting_entity"] == "J P Morgan Bank"
    assert summary["reporting_date"] == "2026-06-30"
    assert summary["submission_version"] == "v1"
    assert summary["file_name"] == "corep_c90.pdf"
    assert summary["datapoint_count"] == 10

    datapoints = body["datapoints"]
    assert len(datapoints) == 10
    assert [d["attribute_name"] for d in datapoints] == ["normalized_usd"] * 10
    assert {d["identifiers"]["normalized_id"] for d in datapoints} == {
        "NORM-SENS-00001", "NORM-SENS-00002", "NORM-SENS-00003", "NORM-SENS-00004",
        "NORM-SENS-00005", "NORM-SENS-00006", "NORM-SENS-00007", "NORM-SENS-00008",
        "NORM-SENS-00009", "NORM-SENS-00010",
    }
    # The supplied 3.18 is preserved even though the formula produces 4.6023.
    mismatch = next(
        d for d in datapoints
        if d["identifiers"]["normalized_id"] == "NORM-SENS-00005"
    )
    assert mismatch["value"] == 3.18


def test_a_report_and_its_datapoints_are_readable(client, report_id: str) -> None:
    detail = client.get(f"/reports/{report_id}").json()

    assert detail["id"] == report_id
    assert detail["datapoint_count"] == 3
    assert detail["file_name"] == "corep_c07.pdf"

    datapoints = client.get(f"/reports/{report_id}/datapoints").json()
    assert {d["row_code"] for d in datapoints} == {"0010", "0040", "0050"}
    assert all(d["report_code"] == "C07.00" for d in datapoints)


def test_the_report_list_is_newest_first(client, report_id: str) -> None:
    body = client.get("/reports").json()

    assert body[0]["id"] == report_id
    assert client.get("/reports").json()[0]["datapoint_count"] == 3


def test_an_unknown_report_is_a_404(client) -> None:
    assert client.get("/reports/report-nope").status_code == 404
    assert client.get("/reports/report-nope/datapoints").status_code == 404


def test_the_pdf_can_be_downloaded_again(client, report_id: str) -> None:
    response = client.get(f"/reports/{report_id}/pdf")

    assert response.status_code == 200
    assert response.content.startswith(b"%PDF-")
    assert len(response.content) > 0