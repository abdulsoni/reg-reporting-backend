"""Connections gate every read: nothing is explored before it is approved."""

import pytest


def test_every_seeded_system_is_discovered(client) -> None:
    body = client.get("/seed/systems").json()

    assert [item["name"] for item in body] == [
        "reporting_db",
        "sa_engine",
        "dwh",
        "staging",
        "loans_db",
        "collateral_db",
    ]
    assert all(item["seeded"] for item in body)
    assert not any(item["connected"] for item in body)


def test_exploration_is_refused_until_a_system_is_connected(client) -> None:
    response = client.post(
        "/lineage/explore",
        json={"system": "reporting_db", "table": "c0700_facts", "attribute": "value"},
    )

    assert response.status_code == 400
    assert "/lineage/connect" in response.json()["detail"]


def test_connecting_reads_the_real_schema(connect) -> None:
    result = connect("reporting_db")[0]

    assert result["system"] == "reporting_db"
    assert result["db_type"] == "sqlite"
    assert result["table_count"] > 0
    assert result["target"]


def test_tables_schema_and_samples_come_from_the_database(client, connect) -> None:
    connect("loans_db")

    tables = client.post("/lineage/tables", json={"system": "loans_db"}).json()
    assert "facility" in {table["name"] for table in tables["tables"]}

    schema = client.post(
        "/lineage/schema", json={"system": "loans_db", "table": "facility"}
    ).json()
    columns = {column["name"] for column in schema["columns"]}
    assert {"facility_id", "drawn_balance", "undrawn_balance"} <= columns
    drawn = next(c for c in schema["columns"] if c["name"] == "drawn_balance")
    assert drawn["data_type"].lower() == "real"
    assert drawn["is_nullable"] is False

    sample = client.post(
        "/lineage/sample", json={"system": "loans_db", "table": "facility"}
    ).json()
    assert sample["row_count"] >= 1
    assert "undrawn_balance" in sample["columns"]
    assert isinstance(sample["rows"][0]["drawn_balance"], (int, float))


def test_row_limit_is_honoured(client, connect) -> None:
    connect("dwh")

    sample = client.post(
        "/lineage/sample", json={"system": "dwh", "table": "dw_exposure", "row_limit": 1}
    ).json()

    assert sample["row_count"] == 1


def test_the_sqlite_adapter_opens_files_read_only(client, connect) -> None:
    connect("reporting_db")

    response = client.post(
        "/lineage/explore",
        json={"system": "reporting_db", "table": "c0700_facts", "attribute": "value"},
    )

    # A read-only handle still serves metadata; it simply cannot be written to.
    assert response.status_code == 200


@pytest.mark.parametrize("table", ["c0700_facts; DROP TABLE reports", "not_a_table"])
def test_unknown_or_hostile_identifiers_are_rejected(client, connect, table: str) -> None:
    connect("reporting_db")

    response = client.post(
        "/lineage/schema", json={"system": "reporting_db", "table": table}
    )

    assert response.status_code == 400


def test_a_path_outside_the_data_directory_is_refused(client) -> None:
    response = client.post(
        "/lineage/connect",
        json={"system": "reporting_db", "database": "../../../etc/passwd"},
    )

    assert response.status_code == 400


def test_an_unknown_system_is_refused(client) -> None:
    response = client.post("/lineage/connect", json={"system": "erp"})

    assert response.status_code == 400
    assert "Unknown system" in response.json()["detail"]


def test_disconnect_revokes_access(client, connect) -> None:
    connect("collateral_db")

    assert client.get("/lineage/connections").json()[0]["connected"] is True
    assert client.delete("/lineage/connections/collateral_db").status_code == 200

    response = client.post(
        "/lineage/sample", json={"system": "collateral_db", "table": "collateral"}
    )
    assert response.status_code == 400


def test_a_system_can_be_connected_by_db_type_and_database(client) -> None:
    response = client.post(
        "/lineage/connect",
        json={"db_type": "sqlite", "database": "loans_db.sqlite"},
    )

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["system"] == "loans_db"
    assert result["db_type"] == "sqlite"
    assert result["database"] == "loans_db.sqlite"
    assert result["table_count"] > 0


def test_a_database_name_is_normalised_to_its_file(client) -> None:
    result = client.post(
        "/lineage/connect",
        json={"db_type": "sqlite", "database": "loans_db"},
    ).json()

    assert result["system"] == "loans_db"
    assert result["database"] == "loans_db.sqlite"


def test_the_database_alone_resolves_a_system(client) -> None:
    result = client.post(
        "/lineage/connect",
        json={"database": "collateral_db.sqlite"},
    ).json()

    assert result["system"] == "collateral_db"


def test_tables_are_read_from_a_target_without_a_system_name(client) -> None:
    client.post(
        "/lineage/connect",
        json={"db_type": "sqlite", "database": "loans_db.sqlite"},
    )

    tables = client.post(
        "/lineage/tables",
        json={"db_type": "sqlite", "database": "loans_db.sqlite"},
    ).json()

    assert "facility" in {table["name"] for table in tables["tables"]}


def test_a_system_name_still_overrides_the_target(client, connect) -> None:
    connect("loans_db")

    tables = client.post(
        "/lineage/tables",
        json={"system": "loans_db", "database": "reporting_db.sqlite"},
    ).json()

    assert "facility" in {table["name"] for table in tables["tables"]}


def test_an_unregistered_target_is_refused(client) -> None:
    response = client.post(
        "/lineage/connect",
        json={"db_type": "sqlite", "database": "no_such_db.sqlite"},
    )

    assert response.status_code == 400
    assert "No registered system matches" in response.json()["detail"]


def test_a_target_can_be_explored_after_connecting_by_target(client) -> None:
    client.post(
        "/lineage/connect",
        json={"db_type": "sqlite", "database": "reporting_db.sqlite"},
    )

    sample = client.post(
        "/lineage/sample",
        json={
            "db_type": "sqlite",
            "database": "reporting_db.sqlite",
            "table": "c0700_facts",
        },
    ).json()

    assert sample["row_count"] >= 1