"""Show what the bot would book for the availability documented in the spec.

Prints to the terminal and saves the same output to reports/estrategia.html.
Reads only the local fixture: no network, no credentials.
"""

import datetime as dt
import json
from pathlib import Path

from rich.console import Console

from asw_bot.models import BOGOTA, Disponibilidad
from asw_bot.strategy import Preferences, build_plan
from asw_bot.views import plan_panel

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "ver_disponibilidad.json"
REPORT = ROOT / "reports" / "estrategia.html"

# The moment the fixture was captured: Monday 9 November 2026, 13:52 Bogota.
NOW = dt.datetime(2026, 11, 9, 13, 52, tzinfo=BOGOTA)
MONDAY = dt.date(2026, 11, 9)
WEEKDAYS = frozenset({1, 2, 3, 4, 5})


def hours(*values: str) -> tuple[dt.time, ...]:
    return tuple(dt.time.fromisoformat(v) for v in values)


def main() -> None:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    full = Disponibilidad.model_validate(payload)
    monday_only = Disponibilidad.model_validate(
        {**payload, "horarios": [d for d in payload["horarios"] if d["fecha"] == "2026-11-09"]}
    )

    scenarios = [
        (
            "1. Preferencias por defecto (06:00, 07:30), sin foto previa",
            full,
            Preferences(hours("06:00", "07:30"), WEEKDAYS),
            None,
        ),
        (
            "2. Una hora que el día no ofrece y dos con pocos cupos (21:00, 15:00, 13:30)",
            full,
            Preferences(hours("21:00", "15:00", "13:30"), WEEKDAYS),
            None,
        ),
        (
            "3. Si solo se viera el lunes: 15:00 está llena, pasa a 16:30",
            monday_only,
            Preferences(hours("15:00", "16:30"), WEEKDAYS),
            None,
        ),
        (
            "4. Igual que el 3, con ALLOW_SECOND_OPTION=true",
            monday_only,
            Preferences(hours("15:00", "16:30"), WEEKDAYS, allow_second_option=True),
            None,
        ),
        (
            "5. Con foto previa {lunes}: el martes es el día nuevo",
            full,
            Preferences(hours("06:00", "07:30"), WEEKDAYS),
            {MONDAY},
        ),
    ]

    console = Console(record=True, width=110)
    console.print(
        f"[bold]Plan de reserva[/] calculado como si fuera {NOW:%Y-%m-%d %H:%M} (Bogotá)\n"
    )
    for title, disponibilidad, preferences, previas in scenarios:
        plan = build_plan(disponibilidad, preferences, [], NOW, previas)
        day = disponibilidad.day(plan.target_date) if plan.target_date else None
        console.print(plan_panel(title, plan, day))
        console.print()

    REPORT.parent.mkdir(exist_ok=True)
    console.save_html(str(REPORT), clear=False)
    console.print(f"[dim]Guardado en {REPORT.relative_to(ROOT)}[/]")


if __name__ == "__main__":
    main()
