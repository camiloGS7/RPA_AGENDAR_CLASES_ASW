"""The plan explanation renders every outcome without errors and says the right thing."""

import datetime as dt
import io

from rich.console import Console, RenderableType

from asw_bot.models import BOGOTA, Disponibilidad
from asw_bot.strategy import Preferences, build_plan
from asw_bot.views import plan_panel
from helpers import load_fixture

MON = dt.date(2026, 11, 9)
TUE = dt.date(2026, 11, 10)
NOW = dt.datetime(2026, 11, 9, 13, 52, tzinfo=BOGOTA)


def render(renderable: RenderableType) -> str:
    console = Console(file=io.StringIO(), record=True, width=110)
    console.print(renderable)
    return console.export_text()


def hours(*values: str) -> tuple[dt.time, ...]:
    return tuple(dt.time.fromisoformat(v) for v in values)


def test_panel_shows_target_order_and_verdicts() -> None:
    disponibilidad = Disponibilidad.model_validate(load_fixture("ver_disponibilidad.json"))
    preferences = Preferences(hours("06:00", "07:30", "21:00"), frozenset({2}))
    plan = build_plan(disponibilidad, preferences, [], NOW)

    text = render(plan_panel("Plan", plan, disponibilidad.day(TUE)))

    assert "martes 2026-11-10" in text
    assert "día más lejano visible" in text
    assert "06:00 -> 07:30" in text
    assert "Candidata: tiene cupos" in text
    assert "No está en TARGET_HOURS" in text
    assert "el día no ofrece esta hora" in text
    assert "#1" in text
    assert "#3" in text


def test_panel_marks_second_option_and_new_day() -> None:
    payload = load_fixture("ver_disponibilidad.json")
    disponibilidad = Disponibilidad.model_validate(payload)
    preferences = Preferences(hours("15:00", "16:30"), frozenset({1}), allow_second_option=True)
    plan = build_plan(disponibilidad, preferences, [], NOW, fechas_previas={TUE})

    text = render(plan_panel("Plan", plan, disponibilidad.day(MON)))

    assert "lunes 2026-11-09" in text
    assert "día nuevo respecto a la foto previa" in text
    assert "16:30 -> 15:00 (segunda opción)" in text
    assert "pedir segunda opción" in text


def test_panel_without_attempts_explains_why() -> None:
    plan = build_plan(None, Preferences(hours("06:00"), frozenset({2})), [], NOW)

    text = render(plan_panel("Plan", plan, None))

    assert "no hay temas pendientes" in text
    assert "Orden de intentos" not in text
