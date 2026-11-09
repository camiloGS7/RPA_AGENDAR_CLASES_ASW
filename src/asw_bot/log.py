"""Logging setup: one JSON object per line, millisecond timestamps, secrets redacted."""

import datetime as dt
import logging
import re
import sys
from typing import Any

import structlog
from structlog.typing import EventDict, WrappedLogger

REDACTED = "[REDACTED]"

# A JWT is three base64url segments; the header always starts with "eyJ" ('{"').
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[^\s\"']+")


def redact(value: Any) -> Any:
    """Replace anything that looks like a token, at any depth."""
    if isinstance(value, str):
        return _BEARER_RE.sub(f"Bearer {REDACTED}", _JWT_RE.sub(REDACTED, value))
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [redact(item) for item in value]
    return value


def redact_secrets(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    """Last line of defence: the code must not log secrets, this catches slips."""
    return {key: redact(item) for key, item in event_dict.items()}


def add_timestamp_ms(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    event_dict["ts"] = dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds")
    return event_dict


def configure_logging(level: int = logging.INFO) -> None:
    """Send JSON logs to stderr, keeping stdout free for the rich output."""
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            add_timestamp_ms,
            redact_secrets,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=False,
    )
