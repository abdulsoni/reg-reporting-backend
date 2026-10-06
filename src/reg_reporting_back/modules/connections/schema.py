"""Request and response models for the connection and exploration endpoints."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from .adapters import SUPPORTED_DB_TYPES

MESSAGE_CONNECTED = "Connection established"


def _strip(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("must not be empty")
    return value


class ConnectRequest(BaseModel):
    """Ask to expose one registered system to the lineage explorer.

    Only ``system`` is required: the db type and database default to the values
    in the system registry, so a request can never point the explorer at an
    arbitrary server. The remaining fields are for database types that need
    them; the password is used to validate the connection and is never stored.
    """

    system: str
    db_type: str | None = None
    database: str | None = None
    host: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    username: str | None = None
    password: SecretStr | None = None
    table_schema: str | None = Field(
        default=None,
        alias="schema",
        description="Schema filter for database types that have schemas",
    )

    model_config = ConfigDict(populate_by_name=True)

    @field_validator("system")
    @classmethod
    def _clean_system(cls, value: str) -> str:
        return _strip(value).lower()

    @field_validator("username")
    @classmethod
    def _blank_username_is_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    def get_password(self) -> str | None:
        return self.password.get_secret_value() if self.password else None


class SystemConnectionInfo(BaseModel):
    """A registered system connection."""

    system_name: str
    db_type: str
    database_name: str
    connected: bool
    connected_at: str | None = None


class ConnectResult(BaseModel):
    status: str = "success"
    message: str = MESSAGE_CONNECTED
    system: str
    db_type: str
    database: str
    target: str = Field(description="Description of what the adapter reached")
    table_count: int = 0
    connected_at: str | None = None


class TableListRequest(BaseModel):
    system: str
    table_schema: str | None = Field(default=None, alias="schema")

    model_config = ConfigDict(populate_by_name=True)

    @field_validator("system")
    @classmethod
    def _clean_system(cls, value: str) -> str:
        return _strip(value).lower()

    def get_schema_name(self) -> str | None:
        return self.table_schema


class TableSchemaRequest(TableListRequest):
    table: str = Field(min_length=1)


class TableSampleRequest(TableSchemaRequest):
    row_limit: int = Field(default=10, ge=1, le=200)


class TableInfo(BaseModel):
    table_schema: str | None = Field(default=None, alias="schema")
    name: str
    type: str

    model_config = ConfigDict(populate_by_name=True)


class TableListResult(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    system: str
    database: str
    table_schema: str | None = Field(default=None, alias="schema")
    count: int
    tables: list[TableInfo]


class ColumnInfo(BaseModel):
    name: str
    data_type: str
    is_nullable: bool
    ordinal_position: int
    default: str | None = None


class TableSchemaResult(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    system: str
    database: str
    table_schema: str | None = Field(default=None, alias="schema")
    table: str
    count: int
    columns: list[ColumnInfo]


class TableSampleResult(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    system: str
    database: str
    table_schema: str | None = Field(default=None, alias="schema")
    table: str
    row_count: int
    columns: list[str]
    rows: list[dict[str, Any]]