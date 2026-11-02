from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


DEFAULT_LMSTUDIO_BASE_URL = "http://lmstudio.lan:1234/v1"


class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="AICMS_",
        extra="ignore",
    )

    app_name: str = "AICMS"
    timezone: str = "US/Central"
    host: str = "0.0.0.0"
    port: int = 8000
    secret_key: str = "change-this-secret"
    data_dir: Path = Path("./data")
    logs_dir: Path = Path("./logs")
    database_url: str | None = None
    default_lmstudio_base_url: str = Field(default=DEFAULT_LMSTUDIO_BASE_URL)
    sync_jobs: bool = False
    ssh_connect_timeout_seconds: int = 10
    ssh_command_timeout_seconds: int = 45
    lmstudio_timeout_seconds: int = 60
    github_timeout_seconds: int = 10

    @field_validator("data_dir", "logs_dir", mode="before")
    @classmethod
    def _normalize_path(cls, value: str | Path) -> Path:
        return Path(value).expanduser()

    @property
    def resolved_data_dir(self) -> Path:
        return self.data_dir.resolve()

    @property
    def resolved_logs_dir(self) -> Path:
        return self.logs_dir.resolve()

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{self.resolved_data_dir / 'aicms.db'}"

    @property
    def templates_dir(self) -> Path:
        return Path(__file__).resolve().parent / "web" / "templates"

    @property
    def static_dir(self) -> Path:
        return Path(__file__).resolve().parent / "web" / "static"


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    return AppConfig()
