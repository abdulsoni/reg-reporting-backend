from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from reg_reporting_back.core.database import create_schema
from reg_reporting_back.modules.connections.routers import router as connections_router
from reg_reporting_back.modules.documents.routers import router as documents_router
from reg_reporting_back.modules.lineage.routers import router as lineage_router
from reg_reporting_back.modules.reports.routers import router as reports_router
from reg_reporting_back.modules.seed.routers import router as seed_router
from fastapi.middleware.cors import CORSMiddleware


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Create the application database tables on first boot."""

    create_schema()
    yield


app = FastAPI(
    title="Regulatory Reporting Backend",
    description=(
        "Report-to-source lineage for regulatory returns: upload a regulatory "
        "return PDF, connect the systems it draws on, and trace a single "
        "reported figure back to the systems of record that produced it."
    ),
    version="0.2.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(seed_router)
app.include_router(documents_router)
app.include_router(reports_router)
app.include_router(connections_router)
app.include_router(lineage_router)


@app.get("/health", tags=["Health"])
def health() -> dict[str, str]:
    return {"status": "ok"}