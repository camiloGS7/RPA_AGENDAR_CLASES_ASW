"""Settings parse the documented .env format and refuse unsafe values."""

import datetime as dt
from pathlib import Path

import pytest
from pydantic import ValidationError

from asw_bot.config import Settings
from helpers import PASSWORD, make_settings

ENV_EXAMPLE = Path(__file__).parent.parent / ".env.example"


def test_env_example_is_loadable_as_is() -> None:
    settings = Settings(_env_file=ENV_EXAMPLE)

    assert settings.asw_base_url == "https://blocks.asw.edu.co/api/student/"
    # Inline comments in the file must not leak into the values.
    assert settings.target_hours == (dt.time(6, 0), dt.time(7, 30))
    assert settings.target_weekdays == (1, 2, 3, 4, 5)
    assert settings.max_classes_per_day == 1
    assert settings.allow_second_option is False
    assert settings.poll_interval_ms == 400
    assert settings.poll_start_offset_ms == -3000
    assert settings.dry_run is True
    assert settings.telegram_bot_token is None
    assert settings.telegram_chat_id is None


def test_dry_run_is_on_unless_explicitly_disabled() -> None:
    assert Settings(_env_file=None).dry_run is True


def test_environment_variables_override_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TARGET_HOURS", "18:00:00, 19:30:00")
    monkeypatch.setenv("TARGET_WEEKDAYS", "2,4")
    monkeypatch.setenv("DRY_RUN", "false")

    settings = Settings(_env_file=None)

    assert settings.target_hours == (dt.time(18, 0), dt.time(19, 30))
    assert settings.target_weekdays == (2, 4)
    assert settings.dry_run is False


def test_password_is_hidden_in_repr() -> None:
    settings = make_settings(asw_token="some-token-value")
    text = repr(settings) + str(settings)

    assert PASSWORD not in text
    assert "some-token-value" not in text
    assert settings.asw_password.get_secret_value() == PASSWORD


def test_base_url_gets_a_trailing_slash() -> None:
    assert make_settings(asw_base_url="http://localhost:8000/api/student").asw_base_url.endswith(
        "/student/"
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"poll_interval_ms": 399},  # faster than the politeness limit
        {"max_classes_per_day": 6},  # above the ASW cap
        {"max_classes_per_day": 0},
        {"target_weekdays": "0,1"},
        {"target_weekdays": "8"},
        {"target_hours": ""},
        {"target_hours": "6am"},
        {"tz_local": "America/Nowhere"},
        {"poll_start_offset_ms": 100},
    ],
)
def test_unsafe_or_invalid_values_are_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        make_settings(**overrides)


def test_timezone_object_matches_the_setting() -> None:
    assert make_settings().tz.key == "America/Bogota"
