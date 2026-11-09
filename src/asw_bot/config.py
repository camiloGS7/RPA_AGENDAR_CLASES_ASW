"""Settings loaded from environment variables and ``.env`` (pydantic-settings).

Validation happens once, at startup: a bad value fails here with a clear
message instead of at 04:40 in the middle of the window.
"""

import datetime as dt
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

ASW_MAX_CLASSES_PER_DAY = 5
MIN_POLL_INTERVAL_MS = 400


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # Credentials. SecretStr keeps the value out of repr() and tracebacks.
    asw_email: str = ""
    asw_password: SecretStr = SecretStr("")
    asw_base_url: str = "https://blocks.asw.edu.co/api/student/"
    # Optional pre-issued token (for Railway, where there is no persistent disk).
    asw_token: SecretStr | None = None
    token_file: Path = Path.home() / ".asw-bot" / "token"
    tz_local: str = "America/Bogota"
    http2: bool = False

    # Booking preferences. NoDecode: these are comma-separated, not JSON.
    target_hours: Annotated[tuple[dt.time, ...], NoDecode] = (dt.time(6, 0), dt.time(7, 30))
    target_weekdays: Annotated[tuple[int, ...], NoDecode] = (1, 2, 3, 4, 5)
    max_classes_per_day: int = Field(default=1, ge=1, le=ASW_MAX_CLASSES_PER_DAY)
    allow_second_option: bool = False

    # Opening window.
    pre_window_seconds: int = Field(default=180, ge=0)
    poll_interval_ms: int = Field(default=400, ge=MIN_POLL_INTERVAL_MS)
    poll_start_offset_ms: int = Field(default=-3000, le=0)
    window_timeout_seconds: int = Field(default=60, gt=0)

    # Safe by default: writes to the real API need an explicit DRY_RUN=false.
    dry_run: bool = True

    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None

    @field_validator("target_hours", "target_weekdays", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("asw_token", "telegram_bot_token", "telegram_chat_id", mode="before")
    @classmethod
    def _empty_as_none(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("target_hours")
    @classmethod
    def _require_hours(cls, value: tuple[dt.time, ...]) -> tuple[dt.time, ...]:
        if not value:
            raise ValueError("TARGET_HOURS needs at least one hour")
        return value

    @field_validator("target_weekdays")
    @classmethod
    def _check_weekdays(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if any(day < 1 or day > 7 for day in value):
            raise ValueError("TARGET_WEEKDAYS must be between 1 (Monday) and 7 (Sunday)")
        return value

    @field_validator("asw_base_url")
    @classmethod
    def _ensure_trailing_slash(cls, value: str) -> str:
        # httpx joins relative paths against the base URL; without the slash
        # "student" would be replaced instead of extended.
        return value if value.endswith("/") else value + "/"

    @field_validator("tz_local")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (KeyError, ValueError) as exc:
            raise ValueError(f"unknown time zone: {value!r}") from exc
        return value

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.tz_local)
