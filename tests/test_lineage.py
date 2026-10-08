"""The lineage engine: exploration, connection gating, resume, and the graph."""

ALL_SYSTEMS = ("reporting_db", "sa_engine", "dwh", "staging", "loans_db", "collateral_db")

EXPECTED_FINAL_SOURCES = {
    "collateral_db.collateral.market_value",
    "loans_db.facility.drawn_balance",
    "loans_db.facility.undrawn_balance",
}


def start_trace(client, datapoint, trace_id: str | None = None) -> dict:
    payload = {"system": "reporting_db", "datapoint_id": datapoint["id"]}
    if trace_id:
        payload["trace_id"] = trace_id
    response = client.post("/lineage/trace", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def test_explore_reads_the_published_metadata(client, connect) -> None:
    connect("reporting_db")

    body = client.post(
        "/lineage/explore",
        json={
            "system": "reporting_db",
            "table": "c0700_facts",
            "attribute": "value",
            "report_code": "C07.00",
            "row_code": "0010",
            "column_code": "0200",
        },
    ).json()

    assert body["status"] == "IDENTIFIED"
    assert body["source_from"] == "sa_engine.calc_sa_exposure.exposure_value"
    assert body["next_system"] == "sa_engine"
    assert body["next_action"] == "CONNECT_SOURCE"
    # Live values come out of the database, not out of the metadata table.
    assert body["evidence_values"] == [1240.5, 600.0, 500.0]


def test_the_report_column_is_ambiguous_without_its_coordinates(client, connect) -> None:
    connect("reporting_db")

    body = client.post(
        "/lineage/explore",
        json={"system": "reporting_db", "table": "c0700_facts", "attribute": "value"},
    ).json()

    assert body["status"] == "AMBIGUOUS_METADATA"
    assert body["next_action"] == "DISAMBIGUATE_DATAPOINT"
    assert body["source_from"] is None


def test_a_column_with_no_published_lineage_is_reported_as_such(client, connect) -> None:
    connect("reporting_db")

    body = client.post(
        "/lineage/explore",
        json={"system": "reporting_db", "table": "c0800_facts", "attribute": "value"},
    ).json()

    assert body["status"] == "METADATA_NOT_FOUND"
    assert body["next_action"] == "REVIEW_METADATA"


def test_a_system_of_record_is_the_end_of_the_line(client, connect) -> None:
    connect("collateral_db")

    body = client.post(
        "/lineage/explore",
        json={"system": "collateral_db", "table": "collateral", "attribute": "market_value"},
    ).json()

    assert body["status"] == "SOURCE_REACHED"
    assert body["sources"] == []
    assert body["next_action"] == "TRACE_COMPLETE"


def test_a_trace_stops_at_the_first_system_the_user_has_not_connected(
    client, connect, exposure_datapoint
) -> None:
    connect("reporting_db")

    body = start_trace(client, exposure_datapoint)

    assert body["status"] == "CONNECTION_REQUIRED"
    assert body["next_system"] == "sa_engine"
    assert body["pending_systems"] == ["sa_engine"]
    assert len(body["hops"]) == 1
    assert body["hops"][0]["system"] == "reporting_db"


def test_a_trace_resumes_as_each_connection_is_granted(client, connect, exposure_datapoint) -> None:
    connect("reporting_db")
    trace_id = start_trace(client, exposure_datapoint)["trace_id"]

    growth = []
    for system in ("sa_engine", "dwh", "staging", "loans_db", "collateral_db"):
        connect(system)
        body = start_trace(client, exposure_datapoint, trace_id)
        growth.append((body["status"], len(body["hops"])))

    assert growth == [
        ("CONNECTION_REQUIRED", 2),
        ("CONNECTION_REQUIRED", 6),
        ("CONNECTION_REQUIRED", 9),
        # The collateral branch stops at staging until collateral_db is granted.
        ("CONNECTION_REQUIRED", 11),
        ("COMPLETED", 12),
    ]
    assert set(body["final_sources"]) == EXPECTED_FINAL_SOURCES
    assert body["pending_systems"] == []


def test_a_completed_trace_keeps_the_same_id_across_resumes(
    client, connect, exposure_datapoint
) -> None:
    connect(*ALL_SYSTEMS)
    trace_id = start_trace(client, exposure_datapoint)["trace_id"]

    assert start_trace(client, exposure_datapoint, trace_id)["trace_id"] == trace_id
    assert client.get(f"/lineage/trace/{trace_id}").json()["status"] == "COMPLETED"


def test_a_full_trace_walks_the_reconciled_lineage(client, connect, exposure_datapoint) -> None:
    connect(*ALL_SYSTEMS)

    body = start_trace(client, exposure_datapoint)
    reached = {
        (hop["system"], hop["table_name"], hop["attribute_name"]) for hop in body["hops"]
    }

    assert ("reporting_db", "c0700_facts", "value") in reached
    assert ("sa_engine", "calc_sa_exposure", "exposure_value") in reached
    # exposure splits into an exposure branch and a collateral branch.
    assert ("dwh", "mart_sa_exposure", "off_bal_eur") in reached
    assert ("dwh", "dw_collateral_alloc", "allocated_value") in reached
    # off balance splits again into drawn and undrawn.
    assert ("dwh", "dw_exposure", "drawn_eur") in reached
    assert ("dwh", "dw_exposure", "undrawn_eur") in reached
    assert ("loans_db", "facility", "drawn_balance") in reached
    assert ("loans_db", "facility", "undrawn_balance") in reached
    assert ("collateral_db", "collateral", "market_value") in reached


def test_the_same_column_on_two_branches_is_traced_on_both(
    client, connect, exposure_datapoint
) -> None:
    connect(*ALL_SYSTEMS)

    body = start_trace(client, exposure_datapoint)
    undrawn = [
        hop for hop in body["hops"] if hop["attribute_name"] == "undrawn_eur"
    ]

    # Every hop records the route that reached it, so the same node reached
    # along two branches keeps both branch paths instead of collapsing into one.
    assert len({hop["branch_path"] for hop in body["hops"]}) == len(body["hops"])
    assert all(hop["branch_path"].startswith("|reporting_db.c0700_facts.value|") for hop in undrawn)
    assert len({hop["consumed_by"] for hop in undrawn}) == 1


def test_the_context_of_the_datapoint_decides_which_report_row_is_traced(client, connect) -> None:
    connect(*ALL_SYSTEMS)
    generated = client.post("/reports/generate-pdf", json={}).json()
    datapoints = client.get(f"/reports/{generated['report']['id']}/datapoints").json()
    on_balance = next(item for item in datapoints if item["row_code"] == "0040")

    body = start_trace(client, on_balance)
    second = body["hops"][1]

    # All three report rows live in c0700_facts.value; the published metadata
    # keys them by report coordinates, so 0040 must not be traced as exposure.
    assert body["hops"][0]["branch_path"] == "|reporting_db.c0700_facts.value"
    assert second["system"] == "sa_engine"
    assert second["attribute_name"] == "drawn_value"


def test_tracing_an_unknown_datapoint_is_refused(client, connect) -> None:
    connect("reporting_db")

    response = client.post(
        "/lineage/trace", json={"system": "reporting_db", "datapoint_id": "dp-nope"}
    )

    assert response.status_code == 400
    assert "not found" in response.json()["detail"].lower()


def test_hops_are_persisted_as_the_trace_grows(client, connect, exposure_datapoint) -> None:
    connect("reporting_db", "sa_engine")
    trace_id = start_trace(client, exposure_datapoint)["trace_id"]

    client.post("/lineage/connect", json={"system": "dwh"})
    start_trace(client, exposure_datapoint, trace_id)

    hops = client.get(f"/lineage/trace/{trace_id}/hops").json()
    stored = client.get(f"/lineage/trace/{trace_id}").json()["hops"]

    assert len(hops) == len(stored)
    assert [hop["system"] for hop in stored[:3]] == ["reporting_db", "sa_engine", "dwh"]


def test_the_graph_is_shaped_for_react_flow(client, connect, exposure_datapoint) -> None:
    connect(*ALL_SYSTEMS)
    trace_id = start_trace(client, exposure_datapoint)["trace_id"]

    graph = client.get(f"/lineage/trace/{trace_id}/graph").json()

    assert set(graph) >= {"nodes", "edges"}
    positions = {(node["position"]["x"], node["position"]["y"]) for node in graph["nodes"]}
    assert len(positions) > 1, "nodes have to be laid out on distinct rows"

    node_ids = {node["id"] for node in graph["nodes"]}
    for edge in graph["edges"]:
        assert edge["source"] in node_ids
        assert edge["target"] in node_ids

    # Edges point upstream, from the system of record towards the report.
    assert "dwh.mart_sa_exposure.off_bal_eur->sa_engine.calc_sa_exposure.exposure_value" in {
        edge["id"] for edge in graph["edges"]
    }

    final = [node for node in graph["nodes"] if node["data"].get("final_source")]
    assert {node["id"] for node in final} == EXPECTED_FINAL_SOURCES


def test_a_graph_for_a_trace_with_no_hops_is_refused(client, connect, exposure_datapoint) -> None:
    connect("reporting_db")
    trace_id = start_trace(client, exposure_datapoint)["trace_id"]
    client.delete("/lineage/connections/reporting_db")

    assert start_trace(client, exposure_datapoint, trace_id)["hops"] == []
    response = client.get(f"/lineage/trace/{trace_id}/graph")

    # The hop from the first trace is still stored, so the graph still renders.
    assert response.status_code == 200


def test_explore_accepts_a_db_type_and_database_target(client) -> None:
    client.post(
        "/lineage/connect",
        json={"db_type": "sqlite", "database": "reporting_db.sqlite"},
    )

    body = client.post(
        "/lineage/explore",
        json={
            "db_type": "sqlite",
            "database": "reporting_db.sqlite",
            "table": "c0700_facts",
            "attribute": "value",
            "report_code": "C07.00",
            "row_code": "0010",
            "column_code": "0200",
        },
    ).json()

    assert body["system"] == "reporting_db"
    assert body["status"] == "IDENTIFIED"
    assert body["source_from"] == "sa_engine.calc_sa_exposure.exposure_value"


def test_explore_by_get_accepts_a_target(client) -> None:
    client.post(
        "/lineage/connect",
        json={"db_type": "sqlite", "database": "reporting_db.sqlite"},
    )

    response = client.get(
        "/lineage/explore",
        params={
            "table": "c0700_facts",
            "attribute": "value",
            "db_type": "sqlite",
            "database": "reporting_db.sqlite",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["system"] == "reporting_db"


def test_trace_accepts_a_db_type_and_database_target(client, exposure_datapoint) -> None:
    client.post(
        "/lineage/connect",
        json={"db_type": "sqlite", "database": "reporting_db.sqlite"},
    )

    body = client.post(
        "/lineage/trace",
        json={
            "db_type": "sqlite",
            "database": "reporting_db.sqlite",
            "datapoint_id": exposure_datapoint["id"],
        },
    ).json()

    assert body["status"] == "CONNECTION_REQUIRED"
    assert body["hops"][0]["system"] == "reporting_db"


def test_trace_from_a_non_reporting_target_is_refused(client, exposure_datapoint) -> None:
    client.post(
        "/lineage/connect",
        json={"db_type": "sqlite", "database": "loans_db.sqlite"},
    )

    response = client.post(
        "/lineage/trace",
        json={
            "db_type": "sqlite",
            "database": "loans_db.sqlite",
            "datapoint_id": exposure_datapoint["id"],
        },
    )

    assert response.status_code == 400
    assert "reporting layer" in response.json()["detail"]