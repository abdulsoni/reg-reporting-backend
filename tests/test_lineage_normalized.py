"""Scenario B: reverse lineage for a normalized sensitivity figure.

A datapoint selected from the normalized sheet starts at ``normalized_db``
instead of ``reporting_db`` and walks back through FX conversion, the approved
adjustment and the raw sensitivity value. The reported figure is also checked
against the formula its source row declares.
"""

import pytest

ALL_NORMALIZED_SYSTEMS = (
    "normalized_db",
    "fx_reference",
    "adjustment_db",
    "raw_sensitivity",
)

# Only the two terminal records and the approval gate have no further source.
EXPECTED_FINAL_SOURCES = {
    "raw_sensitivity.raw_sensitivity.sensitivity_local",
    "adjustment_db.adjustments.approval_status",
    "fx_reference.fx_rates.fx_rate",
}


def start_normalized_trace(
    client,
    datapoint,
    trace_id: str | None = None,
    system: str = "normalized_db",
) -> dict:
    payload = {"system": system, "datapoint_id": datapoint["id"]}
    if trace_id:
        payload["trace_id"] = trace_id
    response = client.post("/lineage/trace", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def test_the_normalized_sheet_publishes_a_normalized_usd_datapoint(normalized_datapoint) -> None:
    assert normalized_datapoint["attribute_name"] == "normalized_usd"
    # The supplied figure is preserved even though it does not reconcile.
    assert normalized_datapoint["value"] == 3.18
    assert normalized_datapoint["identifiers"] == {
        "normalized_id": "NORM-SENS-00005",
        "trade_id": "TRD-0003",
        "sensitivity_type": "DELTA",
    }


def test_explore_normalized_usd_publishes_its_inputs(client, connect) -> None:
    connect("normalized_db")

    body = client.post(
        "/lineage/explore",
        json={
            "system": "normalized_db",
            "table": "normalized_sensitivity",
            "attribute": "normalized_usd",
        },
    ).json()

    assert body["status"] == "IDENTIFIED"
    assert body["source_from"] == "normalized_db.normalized_sensitivity.adjusted_local"
    assert body["sources"] == [
        "normalized_db.normalized_sensitivity.adjusted_local",
        "fx_reference.fx_rates.fx_rate",
    ]
    # The adjusted local value is in the same system; only FX needs connecting.
    assert body["next_system"] == "fx_reference"
    assert body["next_action"] == "CONNECT_SOURCE"
    assert 3.18 in body["evidence_values"]


def test_a_normalized_trace_parks_every_unconnected_system(
    client, connect, normalized_datapoint
) -> None:
    connect("normalized_db")

    body = start_normalized_trace(client, normalized_datapoint)

    assert body["status"] == "CONNECTION_REQUIRED"
    # normalized_usd and the same-system adjusted_local are resolved first.
    assert len(body["hops"]) == 2
    assert [hop["attribute_name"] for hop in body["hops"]] == [
        "normalized_usd",
        "adjusted_local",
    ]
    assert set(body["pending_systems"]) == {
        "fx_reference",
        "raw_sensitivity",
        "adjustment_db",
    }


def test_a_full_normalized_trace_reaches_three_sources(
    client, connect, normalized_datapoint
) -> None:
    connect(*ALL_NORMALIZED_SYSTEMS)

    body = start_normalized_trace(client, normalized_datapoint)

    reached = {
        (hop["system"], hop["table_name"], hop["attribute_name"]) for hop in body["hops"]
    }

    assert body["status"] == "COMPLETED"
    assert reached == {
        ("normalized_db", "normalized_sensitivity", "normalized_usd"),
        ("normalized_db", "normalized_sensitivity", "adjusted_local"),
        ("fx_reference", "fx_rates", "fx_rate"),
        ("raw_sensitivity", "raw_sensitivity", "sensitivity_local"),
        ("adjustment_db", "adjustments", "adjustment_amount_local"),
        ("adjustment_db", "adjustments", "approval_status"),
    }
    assert set(body["final_sources"]) == EXPECTED_FINAL_SOURCES

    # Columns that are not calculation inputs never appear.
    assert ("normalized_db", "normalized_sensitivity", "original_local") not in reached
    assert ("normalized_db", "normalized_sensitivity", "approved_adjustment_local") not in reached
    assert ("fx_reference", "fx_rates", "fx_rate_id") not in reached


def test_the_trace_flags_the_mismatched_figure(
    client, connect, normalized_datapoint
) -> None:
    connect(*ALL_NORMALIZED_SYSTEMS)

    body = start_normalized_trace(client, normalized_datapoint)

    reconciliation = body["reconciliation"]
    assert reconciliation is not None
    assert reconciliation["attribute"] == "normalized_usd"
    assert reconciliation["key"] == {"normalized_id": "NORM-SENS-00005"}
    assert reconciliation["formula"] == "adjusted_local * fx_rate"
    # The supplied value is preserved and the formula is shown alongside it.
    assert reconciliation["expected_value"] == 3.18
    assert reconciliation["derived_value"] == pytest.approx(4.6023, rel=1e-6)
    assert reconciliation["reconciles"] is False


def test_a_reconciling_figure_reconciles(client, connect) -> None:
    connect(*ALL_NORMALIZED_SYSTEMS)

    generated = client.post("/reports/generate-pdf", json={"scenario": "normalized"}).json()
    report_id = generated["report"]["id"]
    datapoints = client.get(f"/reports/{report_id}/datapoints").json()
    datapoint = next(
        item
        for item in datapoints
        if item.get("identifiers", {}).get("normalized_id") == "NORM-SENS-00001"
    )

    body = start_normalized_trace(client, datapoint)

    assert body["status"] == "COMPLETED"
    reconciliation = body["reconciliation"]
    assert reconciliation["reconciles"] is True
    assert reconciliation["derived_value"] == pytest.approx(405.72, rel=1e-6)


def test_a_normalized_trace_resumes_as_connections_are_granted(
    client, connect, normalized_datapoint
) -> None:
    connect("normalized_db")
    trace_id = start_normalized_trace(client, normalized_datapoint)["trace_id"]

    growth = []
    for system in ("fx_reference", "adjustment_db", "raw_sensitivity"):
        connect(system)
        body = start_normalized_trace(client, normalized_datapoint, trace_id, system=system)
        growth.append((body["status"], len(body["hops"])))

    assert growth == [
        # fx_rate is resolved; the adjustment and raw sensitivity still block.
        ("CONNECTION_REQUIRED", 3),
        # adjustment_amount_local and its approval gate are resolved.
        ("CONNECTION_REQUIRED", 5),
        ("COMPLETED", 6),
    ]
    assert set(body["final_sources"]) == EXPECTED_FINAL_SOURCES


def test_a_normalized_trace_graph_is_shaped_for_react_flow(
    client, connect, normalized_datapoint
) -> None:
    connect(*ALL_NORMALIZED_SYSTEMS)
    trace_id = start_normalized_trace(client, normalized_datapoint)["trace_id"]

    graph = client.get(f"/lineage/trace/{trace_id}/graph").json()

    edge_ids = {edge["id"] for edge in graph["edges"]}
    assert (
        "normalized_db.normalized_sensitivity.adjusted_local"
        "->normalized_db.normalized_sensitivity.normalized_usd"
    ) in edge_ids
    assert (
        "adjustment_db.adjustments.approval_status"
        "->adjustment_db.adjustments.adjustment_amount_local"
    ) in edge_ids

    final = {node["id"] for node in graph["nodes"] if node["data"].get("final_source")}
    assert final == EXPECTED_FINAL_SOURCES

    # The reconciliation travels onto the entry node of the graph.
    root = next(
        node for node in graph["nodes"]
        if node["id"] == "normalized_db.normalized_sensitivity.normalized_usd"
    )
    assert root["data"]["reconciliation"]["reconciles"] is False

    # The graph lays the source systems out to the left of the datapoint.
    positions = {(node["position"]["x"], node["position"]["y"]) for node in graph["nodes"]}
    assert len(positions) == len(graph["nodes"])
    assert root["position"]["x"] == max(node["position"]["x"] for node in graph["nodes"])
    approval = next(
        node for node in graph["nodes"]
        if node["id"] == "adjustment_db.adjustments.approval_status"
    )
    sensitivity = next(
        node for node in graph["nodes"]
        if node["id"] == "raw_sensitivity.raw_sensitivity.sensitivity_local"
    )
    assert approval["position"]["x"] < sensitivity["position"]["x"] < root["position"]["x"]
    assert len({node["position"]["x"] for node in graph["nodes"]}) >= 3


def test_a_normalized_trace_cannot_start_from_a_system_without_an_entry(
    client, connect, normalized_datapoint
) -> None:
    connect("raw_sensitivity")

    response = client.post(
        "/lineage/trace",
        json={"system": "raw_sensitivity", "datapoint_id": normalized_datapoint["id"]},
    )

    assert response.status_code == 400
    assert "reporting layer" in response.json()["detail"]
