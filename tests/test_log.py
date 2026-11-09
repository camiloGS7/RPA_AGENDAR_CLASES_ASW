"""Logging emits JSON with millisecond timestamps and redacts token-shaped values."""

import datetime as dt
import json

import pytest
import structlog

from asw_bot.log import REDACTED, configure_logging, redact
from helpers import OLD_TOKEN


def test_redact_removes_tokens_at_any_depth() -> None:
    event = {
        "plain": OLD_TOKEN,
        "header": f"Bearer {OLD_TOKEN}",
        "opaque": "Authorization: bearer abc123",
        "nested": {"list": [f"x {OLD_TOKEN} y", 5]},
        "number": 7,
    }

    cleaned = redact(event)

    assert OLD_TOKEN not in json.dumps(cleaned)
    assert "abc123" not in json.dumps(cleaned)
    assert cleaned["plain"] == REDACTED
    assert cleaned["nested"]["list"][1] == 5
    assert cleaned["number"] == 7


def test_configured_logs_are_json_with_ms_timestamp_and_no_token(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging()

    structlog.get_logger("test").info("something_happened", detail=f"Bearer {OLD_TOKEN}", n=1)

    line = capsys.readouterr().err.strip()
    record = json.loads(line)
    assert record["event"] == "something_happened"
    assert record["level"] == "info"
    assert record["n"] == 1
    assert OLD_TOKEN not in line
    timestamp = dt.datetime.fromisoformat(record["ts"])
    assert timestamp.tzinfo is not None
    # Millisecond resolution: exactly three fractional digits.
    assert len(record["ts"].split(".")[1].split("+")[0]) == 3


def test_debug_is_filtered_at_info_level(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging()
    structlog.get_logger("test").debug("noise")
    assert capsys.readouterr().err == ""
