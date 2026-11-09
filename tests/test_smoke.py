"""Phase 0 smoke test: the package installs, imports and exposes its entry point."""

import pytest

import asw_bot
from asw_bot.cli import main


def test_package_exposes_version() -> None:
    assert asw_bot.__version__ == "0.1.0"


def test_cli_prints_version_and_succeeds(capsys: pytest.CaptureFixture[str]) -> None:
    assert main() == 0
    assert capsys.readouterr().out.strip() == "asw-bot 0.1.0"
