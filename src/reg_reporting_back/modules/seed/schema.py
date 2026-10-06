"""Request and response models for the seed endpoints."""

from typing import Literal

from pydantic import BaseModel, Field

from ...core.systems import SystemDefinition

MESSAGE_SEEDED = "Demo system databases created"
MESSAGE_RESET = "Demo system databases and application data reset"


class SystemInfo(BaseModel):
    """A system the explorer can connect to, and whether it is connected."""

    name: str
    db_type: str
    database_name: str
    database_file: str
    label: str
    layer: str
    seeded: bool = Field(description="True when the demo database has been built")
    connected: bool = False


class SeedSystemsResult(BaseModel):
    status: Literal["success"] = "success"
    message: str
    systems: list[SystemInfo]
    data_dir: str


def to_system_info(
    definition: SystemDefinition,
    seeded: bool,
    connected: bool,
) -> SystemInfo:
    return SystemInfo(
        name=definition.name,
        db_type=definition.db_type,
        database_name=definition.database_name,
        database_file=definition.database_file,
        label=definition.label,
        layer=definition.layer,
        seeded=seeded,
        connected=connected,
    )