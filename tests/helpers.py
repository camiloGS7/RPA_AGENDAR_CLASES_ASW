"""Shared constants and builders for the tests. Nothing here is a real credential."""

import json
from pathlib import Path
from typing import Any

from asw_bot.config import Settings

FIXTURES = Path(__file__).parent / "fixtures"

# A host that does not exist: even an unmocked request could never reach ASW.
BASE_URL = "https://asw.test/api/student/"

# Shaped like real JWTs so the redaction and leak tests are meaningful.
OLD_TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJ1aWQiOjEsImRhdGEiOiJvbGQifQ.b2xkLXNpZ25hdHVyZQ"
NEW_TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJ1aWQiOjEsImRhdGEiOiJuZXcifQ.bmV3LXNpZ25hdHVyZQ"
EMAIL = "student@example.com"
PASSWORD = "not-a-real-password-9f3a"

INVALID_TOKEN_401 = {
    "message": "E_INVALID_JWT_TOKEN: invalid token",
    "name": "InvalidJwtToken",
    "code": "E_INVALID_JWT_TOKEN",
    "status": 401,
}


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def make_settings(**overrides: Any) -> Settings:
    """Settings that never read the developer's .env and never point at ASW."""
    values: dict[str, Any] = {
        "asw_email": EMAIL,
        "asw_password": PASSWORD,
        "asw_base_url": BASE_URL,
        "dry_run": False,
    }
    return Settings(_env_file=None, **(values | overrides))
