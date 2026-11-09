"""Fixtures shared by every test module."""

import os
from collections.abc import AsyncIterator, Iterator

import pytest
import respx
import structlog

from asw_bot.client import AswClient
from asw_bot.config import Settings
from asw_bot.token_store import MemoryTokenStore
from helpers import BASE_URL, OLD_TOKEN, make_settings

_SETTINGS_ENV_VARS = [name.upper() for name in Settings.model_fields]


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No test may pick up real credentials or settings from the developer's shell."""
    for name in _SETTINGS_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    assert not any(name in os.environ for name in _SETTINGS_ENV_VARS)
    yield
    structlog.reset_defaults()


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def store() -> MemoryTokenStore:
    return MemoryTokenStore(OLD_TOKEN)


@pytest.fixture
def api() -> Iterator[respx.MockRouter]:
    """Mocked ASW API. Any request to a route that is not declared fails the test."""
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
        yield router


@pytest.fixture
async def client(settings: Settings, store: MemoryTokenStore) -> AsyncIterator[AswClient]:
    async with AswClient(settings, store, retry_wait_s=0) as instance:
        yield instance
