"""The run report renders for every kind of result and contains the key facts."""

import datetime as dt
from pathlib import Path

from asw_bot.models import BOGOTA
from asw_bot.report import build_run_report, write_run_report
from asw_bot.runner import RunResult, RunStatus
from test_runner import run_window

T = dt.datetime(2026, 11, 10, 4, 40, tzinfo=BOGOTA)


async def test_report_of_a_successful_run(tmp_path: Path) -> None:
    result, state = await run_window(
        config={"stolen_after_view": frozenset({"07:30:00"})},
        settings={"target_hours": "07:30:00,09:00:00,10:30:00,12:00:00"},
    )
    competitor = {dt.time(9, 0): [(0.0, state.competitor_seats("09:00:00", T)), (500.0, 20)]}

    path = write_run_report(
        result,
        tmp_path / "nested" / "run.html",
        title="Prueba",
        subtitle="sub",
        competitor=competitor,
    )
    page = path.read_text(encoding="utf-8")

    assert page.startswith("<!doctype html>")
    assert "Plotly.newPlot" in page
    assert "Apertura detectada" in page
    assert "+200 ms" in page  # detection in virtual time: first poll after T
    assert "3 de 3" in page
    assert "Peticiones enviadas" in page
    assert "Cupos de tus horas" in page
    assert "No hay cupos disponibles para este horario." in page
    assert "booked_all" in page
    assert page.count("<tr>") > len(result.requests)


async def test_report_of_a_dry_run_and_of_a_failed_login() -> None:
    dry, _ = await run_window(settings={"dry_run": True})
    failed, _ = await run_window(config={"password": "the-right-one"})

    dry_page = build_run_report(dry, title="Ensayo")
    failed_page = build_run_report(failed, title="Fallo")

    assert "Simulada (dry-run)" in dry_page
    assert "simuladas (dry-run)" in dry_page
    assert "AuthError" in failed_page
    assert "No hubo intentos de reserva." in failed_page
    assert "Plotly.newPlot" not in failed_page  # nothing to chart without an opening time


def test_report_of_an_empty_result() -> None:
    page = build_run_report(RunResult(status=RunStatus.FAILED, reason="<script>"), title="Vacío")

    assert "&lt;script&gt;" in page
    assert "desconocida" in page
