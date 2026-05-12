from __future__ import annotations

import os
import tomllib
from pathlib import Path

from pydantic import BaseModel, Field, field_validator


class FastmailConfig(BaseModel):
    token_env: str = "FASTMAIL_TOKEN"
    mailbox: str = "Substacks"
    session_url: str = "https://api.fastmail.com/jmap/session"


class PollConfig(BaseModel):
    interval_seconds: int = Field(default=900, ge=30)
    initial_backfill: int = Field(default=50, ge=1, le=500)
    max_changes: int = Field(default=200, ge=1, le=1000)


class OutputConfig(BaseModel):
    dir: Path = Path("/var/lib/mail2rss/feeds")
    aggregate_filename: str = "all.xml"
    max_entries_per_feed: int = Field(default=100, ge=1, le=1000)


class HttpConfig(BaseModel):
    bind: str = "127.0.0.1:8080"
    feed_url_path_prefix: str = "/feeds"
    require_secret: bool = True

    @field_validator("feed_url_path_prefix")
    @classmethod
    def normalize_prefix(cls, value: str) -> str:
        if not value.startswith("/"):
            value = f"/{value}"
        return value.rstrip("/") or "/feeds"


class FilterConfig(BaseModel):
    require_list_id: bool = False
    require_canonical_url: bool = False
    subject_blocklist: list[str] = Field(default_factory=list)
    from_blocklist: list[str] = Field(default_factory=list)


class LogConfig(BaseModel):
    level: str = "info"
    format: str = "json"


class AppConfig(BaseModel):
    fastmail: FastmailConfig = Field(default_factory=FastmailConfig)
    poll: PollConfig = Field(default_factory=PollConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)
    http: HttpConfig = Field(default_factory=HttpConfig)
    filters: FilterConfig = Field(default_factory=FilterConfig)
    log: LogConfig = Field(default_factory=LogConfig)
    db_path: Path | None = None

    @property
    def database_path(self) -> Path:
        return self.db_path or self.output.dir.parent / "mail2rss.db"

    @property
    def token(self) -> str:
        token = os.environ.get(self.fastmail.token_env)
        if not token:
            raise ConfigError(
                f"Missing Fastmail token env var: {self.fastmail.token_env}"
            )
        return token

    def validate_paths(self) -> None:
        try:
            self.output.dir.mkdir(parents=True, exist_ok=True)
            db_parent = self.database_path.parent
            db_parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ConfigError(f"Unable to prepare state directories: {exc}") from exc
        if not os.access(self.output.dir, os.W_OK):
            raise ConfigError(f"Output dir is not writable: {self.output.dir}")
        if not os.access(db_parent, os.W_OK):
            raise ConfigError(f"Database dir is not writable: {db_parent}")


class ConfigError(RuntimeError):
    pass


def load_config(path: Path | None) -> AppConfig:
    if path is None:
        return AppConfig()
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    return AppConfig.model_validate(data)
