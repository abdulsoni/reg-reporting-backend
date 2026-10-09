"""Shared fixtures.

The application resolves its SQLite files from ``settings.data_dir``, so the
environment is pointed at a temporary directory before the package is imported.
That keeps the tests away from the demo databases in ``src/reg_reporting_back``.
"""

import os
import shutil
import tempfile
from pathlib import Path

_TEST_DATA_DIR = tempfile.mkdtemp(prefix="reg-reporting-tests-")
os.environ["DATA_DIR"] = _TEST_DATA_DIR
os.environ["DATABASE_URL"] = ""

import pytest  # noqa: E402


def pytest_sessionfinish() -> None:
    shutil.rmtree(_TEST_DATA_DIR, ignore_errors=True)


@pytest.fixture(scope="session")
def data_dir() -> Path:
    return Path(_TEST_DATA_DIR)


@pytest.fixture(scope="session")
def seeded(data_dir: Path):
    """Create the six demo system databases once for the whole session."""

    from reg_reporting_back.modules.seed.service import seed_all

    seed_all()
    return data_dir


@pytest.fixture(autouse=True)
def app_database(seeded: Path):
    """Give every test an empty application database."""

    from reg_reporting_back.core.database import reset_application_data

    reset_application_data()


@pytest.fixture
def client(app_database: Path):
    from fastapi.testclient import TestClient

    from reg_reporting_back.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def connect(client):
    """Connect systems the way the UI would, one approval at a time."""

    def _connect(*systems: str) -> list:
        responses = []
        for system in systems:
            response = client.post("/lineage/connect", json={"system": system})
            assert response.status_code == 200, response.text
            responses.append(response.json())
        return responses

    return _connect


@pytest.fixture
def exposure_datapoint(client):
    """Generate a report and return its C07.00 / 0010 exposure datapoint."""

    generated = client.post("/reports/generate-pdf", json={}).json()
    report_id = generated["report"]["id"]
    datapoints = client.get(f"/reports/{report_id}/datapoints").json()
    return next(item for item in datapoints if item["row_code"] == "0010")


@pytest.fixture
def normalized_datapoint(client):
    """Generate the normalized sensitivity sheet and return the NORM-SENS-00005 figure.

    That row is the deliberate mismatch: the sheet publishes 3.18 while the
    declared formula produces 4.6023.
    """

    generated = client.post("/reports/generate-pdf", json={"scenario": "normalized"}).json()
    report_id = generated["report"]["id"]
    datapoints = client.get(f"/reports/{report_id}/datapoints").json()
    return next(
        item for item in datapoints
        if item.get("identifiers", {}).get("normalized_id") == "NORM-SENS-00005"
    )