# Regulatory Reporting Backend

FastAPI service that answers one question: **where did this number in a
regulatory return come from?**

A COREP-style return is uploaded or generated, its figures are extracted as
datapoints, and each datapoint is traced back through every system that has
been connected to it, one system at a time, until it reaches a system of record.

```
report PDF ──▶ datapoint ──▶ trace ──▶ lineage graph
                    ▲           │
                    └── connections, granted one at a time
```

## How it works

1. **Reports.** A PDF is parsed with `pdfplumber`. A table is only turned into
   datapoints when its header row names the coordinates a lineage trace needs
   (`Row`, `Column`, `Attribute`, `Value`), so an unrelated table on the same
   page is ignored instead of producing nonsense.
2. **Connections.** Nothing is read from a client system until the user
   approves it. `POST /lineage/connect` opens a read-only handle, records the
   target, and the explorer then serves real schema and sample data.
3. **Lineage metadata.** Each system publishes a `lineage_metadata` table
   describing where its columns come from. It lives *inside* the system being
   described, so lineage is owned by the system that owns the data.
4. **Tracing.** The tracer walks `system.table.attribute` nodes along the
   published input references. It follows every input of a transformation
   recursively, so a datapoint resolves to its whole dependency graph. It only
   steps into systems that are already connected; anything else is parked in
   `pending_systems` and reported back, so the UI can ask for the next approval.
   Passing `trace_id` resumes.

Connections and lineage metadata are deliberately separate: connecting a system
says "you may read this", while its metadata says "this is where the data came
from". Neither implies the other.

## Requirements

- Python 3.14+
- [uv](https://docs.astral.sh/uv/)
- A Microsoft SQL Server ODBC driver, only if you point the service at a real
  SQL Server (see [ODBC driver selection](#odbc-driver-selection))

## Setup

```powershell
uv sync
Copy-Item .env.example .env   # optional; every setting has a default
```

## Run

```powershell
uv run fastapi dev main.py
```

- API: http://127.0.0.1:8000
- Swagger UI: http://127.0.0.1:8000/docs
- Health check: http://127.0.0.1:8000/health

## Try it end to end

```powershell
# 1. Build the six demo system databases
curl.exe -X POST http://127.0.0.1:8000/seed/databases

# 2. Generate a report, or upload your own PDF
curl.exe -X POST http://127.0.0.1:8000/reports/generate-pdf -H "Content-Type: application/json" -d '{}'
curl.exe -X POST http://127.0.0.1:8000/documents/upload-pdf -F "file=@corep_c07.pdf"

# 3. Approve a system, then look at what it exposes
curl.exe -X POST http://127.0.0.1:8000/lineage/connect -H "Content-Type: application/json" -d '{"db_type":"sqlite","database":"reporting_db.sqlite"}'
curl.exe -X POST http://127.0.0.1:8000/lineage/tables -H "Content-Type: application/json" -d '{"db_type":"sqlite","database":"reporting_db.sqlite"}'

# 4. Trace a datapoint, approving each system it stops at, then resume
curl.exe -X POST http://127.0.0.1:8000/lineage/trace -H "Content-Type: application/json" -d '{"db_type":"sqlite","database":"reporting_db.sqlite","datapoint_id":"<dp id>"}'
curl.exe -X POST http://127.0.0.1:8000/lineage/connect -H "Content-Type: application/json" -d '{"db_type":"sqlite","database":"sa_engine.sqlite"}'
# Resume with just the trace: the tracer auto-continues whatever is connected now
curl.exe -X POST http://127.0.0.1:8000/lineage/trace -H "Content-Type: application/json" -d '{"datapoint_id":"<dp id>","trace_id":"<trace id>"}'

# 5. Render the result
curl.exe http://127.0.0.1:8000/lineage/trace/<trace id>/graph
```

## Demo systems

`POST /seed/databases` writes ten small SQLite databases to `DATA_DIR`. They
form two independent scenarios. The figures reconcile, so the lineage a trace
produces is verifiable by hand -- except for the one deliberate mismatch
Scenario B uses to demonstrate a flagged calculation error:

| System | Role |
| --- | --- |
| `reporting_db` | the regulatory return itself |
| `sa_engine` | standardised approach calculation |
| `dwh` | enterprise warehouse, including two same-system hops |
| `staging` | integration staging |
| `loans_db` | loans system of record |
| `collateral_db` | collateral system of record |
| `normalized_db` | normalized sensitivity engine (Scenario B) |
| `adjustment_db` | sensitivity adjustment system (Scenario B) |
| `raw_sensitivity` | raw sensitivity system of record (Scenario B) |
| `fx_reference` | FX reference feed (Scenario B) |

```
exposure 1240.50 = off balance 1100.00 + allocated collateral 140.50
off balance 1100.00 = undrawn 500.00 + drawn 600.00
```

So each datapoint is traced through every input of every transformation until
each branch reaches a system of record. The exposure datapoint resolves to two
branches:

```
reporting_db.c0700_facts.value                    (reported 1240.50)
└─ sa_engine.calc_sa_exposure.exposure_value
   ├─ dwh.mart_sa_exposure.off_bal_eur
   │  ├─ dwh.dw_exposure.undrawn_eur ─ staging ─ loans_db.facility.undrawn_balance
   │  └─ dwh.dw_exposure.drawn_eur   ─ staging ─ loans_db.facility.drawn_balance
   └─ dwh.dw_collateral_alloc.allocated_value
      └─ staging.stg_collateral.market_value ─ collateral_db.collateral.market_value
```

Only dependencies of the selected datapoint appear; unrelated columns of the
same systems are never walked.

`reporting_db` also holds a `c0800_facts` column with no published lineage, used
to exercise the "no metadata" path.

### Scenario B: normalized sensitivity

A datapoint selected from the normalized sensitivity sheet starts at
`normalized_db` instead of `reporting_db`. Any system that publishes an entry
column can start a trace, so a newer entry point only has to be added to the
registry.

`{"scenario":"normalized"}` renders a full ten-row C 90.00 sheet, one row per
normalized sensitivity:

| Normalized ID | Trade ID | Sensitivity Type | Normalized USD |
| --- | --- | --- | --- |
| NORM-SENS-00001 | TRD-0001 | DELTA | 405.72 |
| ... | | | |
| NORM-SENS-00010 | TRD-0006 | DELTA | 829.92 |

Each datapoint carries its identifying columns as `identifiers`, so the tracer
can locate the exact source row. Every row but NORM-SENS-00005 reconciles:

```
adjusted_local 460.23 = original_local 500.25 + approved_adjustment_local -40.02
normalized_usd   4.60 = adjusted_local 460.23 * fx_rate 0.01
```

NORM-SENS-00005 is the deliberate exception: the sheet publishes `3.18` while
the formula produces `4.6023`. The reported figure is never rewritten; the trace
returns a `reconciliation` alongside the hops:

```json
{
  "attribute": "normalized_usd",
  "key": {"normalized_id": "NORM-SENS-00005"},
  "formula": "adjusted_local * fx_rate",
  "expected_value": 3.18,
  "derived_value": 4.6023,
  "reconciles": false
}
```

The formula comes from the `transformation` the source system publishes and is
evaluated safely over the matching source row, so a verbose or hostile metadata
string simply produces no check rather than an error.

```
normalized_db.normalized_sensitivity.normalized_usd        (reported 3.18)
├─ normalized_db.normalized_sensitivity.adjusted_local
│  ├─ raw_sensitivity.raw_sensitivity.sensitivity_local
│  └─ adjustment_db.adjustments.adjustment_amount_local
│     └─ adjustment_db.adjustments.approval_status
└─ fx_reference.fx_rates.fx_rate
```

The join/filter keys (`sensitivity_id`, `fx_rate_id`) stay context inside the
transformation text; only columns the transformation reads become nodes. The
approval gate is published as a dependency of the adjustment. The trace stops
at three sources: the raw sensitivity value, the FX rate actually used, and the
approval status.

```
# Generate the normalized sheet, then start the trace from that system
curl.exe -X POST http://127.0.0.1:8000/reports/generate-pdf -H "Content-Type: application/json" -d '{"scenario":"normalized"}'
curl.exe -X POST http://127.0.0.1:8000/lineage/connect -H "Content-Type: application/json" -d '{"system":"normalized_db"}'
curl.exe -X POST http://127.0.0.1:8000/lineage/trace -H "Content-Type: application/json" -d '{"system":"normalized_db","datapoint_id":"<normalized_usd dp id>"}'
```


### Lineage metadata

Each seeded database carries a `lineage_metadata` table:

| Column | Purpose |
| --- | --- |
| `table_name`, `attribute_name` | the column this row describes |
| `report_code`, `row_code`, `column_code` | report coordinates, where they apply |
| `source_refs_json` | upstream columns; a list, because one value can have several inputs |
| `consumed_by` | the column that reads this one |
| `transformation` | how the value was derived, shown on the hop |

`c0700_facts.value` is one column with three metadata rows, keyed by report
coordinates, so tracing a figure needs the datapoint's coordinates. Exploring it
without them returns `AMBIGUOUS_METADATA` rather than guessing.

`source_refs_json` is a list so that a column can have more than one input:

```json
["dwh.mart_sa_exposure.off_bal_eur", "dwh.dw_collateral_alloc.allocated_value"]
```

The tracer walks every entry, so a column with several inputs produces several
branches and a trace is the full dependency graph of the datapoint.

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Liveness |
| `GET` | `/seed/systems` | Systems available to connect, and whether they are seeded |
| `POST` | `/seed/databases` | Rebuild the demo databases |
| `POST` | `/seed/reset` | Empty reports, traces and connections |
| `POST` | `/documents/upload-pdf` | Upload a return; parses it and stores it as a report |
| `POST` | `/documents/parse-pdf` | Parse without storing |
| `POST` | `/reports/generate-pdf` | Render a report, store it, return its datapoints |
| `GET` | `/reports` | Stored reports, newest first |
| `GET` | `/reports/{id}` | One report with its datapoint count |
| `GET` | `/reports/{id}/datapoints` | The datapoints a trace starts from |
| `GET` | `/reports/{id}/pdf` | Download the stored PDF |
| `POST` | `/lineage/connect` | Approve a system and open a read-only handle |
| `GET` | `/lineage/connections` | Registered connections and their state |
| `DELETE` | `/lineage/connections/{system}` | Revoke a connection |
| `POST` | `/lineage/tables` | Tables and views the system exposes |
| `POST` | `/lineage/schema` | Column definitions of one table |
| `POST` | `/lineage/sample` | A read-only sample of rows |
| `GET`/`POST` | `/lineage/explore` | Resolve one column: where it came from, and what to do next |
| `POST` | `/lineage/trace` | Start or resume a trace |
| `GET` | `/lineage/trace/{id}` | A stored trace |
| `GET` | `/lineage/trace/{id}/hops` | The hop table |
| `GET` | `/lineage/trace/{id}/graph` | React Flow nodes and edges |

### Identifying a system

Endpoints that read a client system (`/lineage/connect`, `/tables`, `/schema`,
`/sample`, `/explore`, `/trace`) take a target: either `system`, or the pair
`db_type` + `database`. The pair is resolved against the system registry, so the
UI can offer a connection-type dropdown and send what the user picked:

```json
{ "db_type": "sqlite", "database": "reporting_db.sqlite" }
```

`database` matches either the registry name (`reporting_db`) or its file
(`reporting_db.sqlite`), and `db_type` is the connection type shown in the UI
(`sqlite` today, alongside `mssql`). A pair that matches no registered system is
refused, so a request can never point the explorer at an arbitrary system; `api`
is not a readable database and is rejected the same way. `system` wins when both
are sent, which keeps older requests and explicit overrides working.

### Exploring a column

`POST /lineage/explore` answers with a status and, when relevant, a next action:

| `status` | `next_action` | Meaning |
| --- | --- | --- |
| `IDENTIFIED` | `CONNECT_SOURCE` | Its upstream is known but not connected yet |
| `SOURCE_REACHED` | `TRACE_COMPLETE` | A system of record |
| `AMBIGUOUS_METADATA` | `DISAMBIGUATE_DATAPOINT` | Several metadata rows match; pass report coordinates |
| `METADATA_NOT_FOUND` | `REVIEW_METADATA` | The system publishes no lineage for this column |

### Tracing

```json
{ "db_type": "sqlite", "database": "reporting_db.sqlite", "datapoint_id": "dp-…", "trace_id": "trace-…" }
```

`system` may be sent instead of `db_type` + `database` (see
[Identifying a system](#identifying-a-system)). `trace_id` is only needed to
resume. On resume the target is optional: the tracer follows the stored lineage
and continues every pending node whose system is connected now, so resuming with
just `trace_id` + `datapoint_id` advances the trace. Resuming when nothing is
pending returns the trace unchanged instead of failing. The response reports
what the tracer could reach and what it needs next:

```json
{
  "trace_id": "trace-…",
  "status": "CONNECTION_REQUIRED",
  "hops": [ … ],
  "next_system": "sa_engine",
  "pending_systems": ["sa_engine"],
  "final_sources": []
}
```

`status` is `COMPLETED` once the chain has reached a system of record.
`CONNECTION_REQUIRED` means the trace is resumable as-is. `METADATA_NOT_FOUND`
means a system is connected but publishes nothing for a column it should.

Each hop carries the `branch_path` it was reached by. Because a dependency is
followed once, every hop has a distinct path from the datapoint to the source.

### Graph output

`GET /lineage/trace/{id}/graph` returns React Flow nodes and edges and nothing
else, so the frontend does not have to understand any of the lineage rules.
Edges point upstream, from the system of record towards the report.

## Tests

```powershell
uv run pytest
```

The suite runs fully offline against temporary copies of the seeded databases.
It covers the PDF round trip, connection gating and revocation, identifier
safety, ambiguity handling, the dependency-graph trace (both the COREP and
normalized-sensitivity scenarios), resumption one connection at a time, and the
graph projection.

## Layout

```
main.py                                     ASGI entrypoint
src/reg_reporting_back/
  main.py                                   app, router wiring, lifespan
  core/
    config.py                               settings from env / .env
    database.py                             this service's SQLite schema
    exceptions.py                           error hierarchy mapped to HTTP
    routing.py                              one place that turns errors into responses
    systems.py                              the systems that can be connected
  modules/
    documents/                              PDF reading
      routers.py  service.py  schema.py
    reports/                                reports and their datapoints
      routers.py  service.py  generator.py  extractor.py  repository.py  schema.py
    connections/                            approval and read-only exploration
      routers.py  service.py  repository.py  schema.py
      adapters/  base.py  sqlite.py  mssql.py
    lineage/                                exploration and tracing
      routers.py  service.py  explorer.py  repository.py  schema.py
    seed/                                   the demo databases
      routers.py  service.py  schema.py
scripts/
  seed_databases.py                         rebuild the demo systems from the CLI
tests/
```

Dependency direction: `documents` and `seed` know nothing about lineage;
`reports` knows nothing about connections; `lineage` reads reports and
connections. Connections never talk to a client system directly — they go
through an adapter.

## Connecting a real database

The demo systems are SQLite. To point a system at SQL Server, send its
connection details with the approval:

```json
{
  "system": "reporting_db",
  "db_type": "mssql",
  "database": "COREP",
  "host": "sql01\\COREP",
  "port": 1433,
  "username": "lineage_ro",
  "password": "…",
  "schema": "dbo"
}
```

`db_type` accepts `sqlite` and `mssql`. Omit `username` and `password` to use
Windows integrated authentication. Passwords are used to open the connection and
then discarded: they are never written to the application database, so a
non-SQLite system has to present them again on each request.

SQLite targets are file names inside `DATA_DIR`; a path that tries to escape it
is rejected.

### ODBC driver selection

`pyodbc` needs a Microsoft SQL Server ODBC driver, and the driver name differs
between machines. The adapter picks one instead of hardcoding it:

1. `MSSQL_ODBC_DRIVER` from the environment, if set.
2. Otherwise the first installed match: `ODBC Driver 18 for SQL Server`,
   `ODBC Driver 17 for SQL Server`, `SQL Server Native Client 11.0`,
   `SQL Server`.

```powershell
uv run python -c "import pyodbc; print(pyodbc.drivers())"
```

`host` accepts a hostname plus `port` (sent as `host,port`) or a named instance
such as `sql01\COREP`, in which case `port` is ignored.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATA_DIR` | `src/reg_reporting_back/data` | Demo databases and this service's own SQLite file |
| `DATABASE_URL` | `""` | SQLAlchemy URL for this service's database; empty uses `<DATA_DIR>/lineage_app.sqlite` |
| `MSSQL_ODBC_DRIVER` | `None` | Force an ODBC driver; auto-detect when empty |
| `DB_CONNECT_TIMEOUT_SECONDS` | `10` | Connection timeout |
| `MAX_UPLOAD_SIZE_MB` | `20` | Upload cap for `/documents/upload-pdf` |
| `MAX_TRACE_DEPTH` | `32` | Hop limit, so a cycle in published metadata cannot spin forever |

## Errors

| Status | Cause |
| --- | --- |
| 400 | System not connected, unknown system, no lineage for a column, invalid identifier, unreadable PDF, no datapoint table found |
| 404 | Unknown report |
| 413 | Upload larger than `MAX_UPLOAD_SIZE_MB` |
| 422 | Missing or malformed request fields |

Ambiguity and missing metadata are answers rather than errors: they come back
from `/lineage/explore` as a status the UI can act on.

## Security notes

- Passwords are `SecretStr`, so they stay out of `repr`, logs and the OpenAPI
  schema, and are never stored or echoed in an error message.
- SQLite systems are opened with `file:…?mode=ro`; SQL Server handles are
  disposed after the request. Nothing is held between requests.
- `schema`, `table` and `attribute` are validated against
  `^[A-Za-z_][A-Za-z0-9_]*$` before reaching a query, and there is no endpoint
  that accepts caller-supplied SQL.
- A system must be connected before it is read, and revoking a connection
  revokes access immediately.
- There is no authentication on the connection endpoints: anyone who can reach
  the API can point it at a database. Put authentication and an allowlist of
  permitted servers in front of them before deploying.