from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# src/reg_reporting_back/core/config.py -> src/reg_reporting_back/data
PACKAGE_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Application settings loaded from the environment or a .env file.

    Every field has a default so importing this module never fails when no
    .env file is present.
    """

    DATABASE_URL: str = ""
    DATA_DIR: str = ""
    MSSQL_ODBC_DRIVER: str | None = None
    DB_CONNECT_TIMEOUT_SECONDS: int = 10
    MAX_UPLOAD_SIZE_MB: int = 20
    MAX_TRACE_DEPTH: int = 32

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    @property
    def data_dir(self) -> Path:
        """Directory holding the seeded SQLite system databases."""

        return Path(self.DATA_DIR).resolve() if self.DATA_DIR else PACKAGE_ROOT / "data"

    @property
    def app_database_url(self) -> str:
        """SQLAlchemy URL for this service's own SQLite database."""

        if self.DATABASE_URL:
            return self.DATABASE_URL
        return f"sqlite:///{self.data_dir / 'lineage_app.sqlite'}"

    @property
    def max_upload_size_bytes(self) -> int:
        return self.MAX_UPLOAD_SIZE_MB * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()