"""Rich renderables that explain decisions to a human. Presentation only, no logic."""

import datetime as dt

from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from asw_bot.models import Dia
from asw_bot.strategy import AttemptKind, Outcome, Plan, TargetReason, Verdict

_WEEKDAYS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")

_VERDICT_TEXT: dict[Verdict, tuple[str, str]] = {
    Verdict.BOOK: ("Candidata: tiene cupos", "green"),
    Verdict.SECOND_OPTION: ("Llena: pedir segunda opción (siguiente día hábil)", "yellow"),
    Verdict.FULL: ("Descartada: sin cupos", "red"),
    Verdict.CLOSED: ("Descartada: franja cerrada", "red"),
    Verdict.TOO_LATE: ("Descartada: falta menos de 1 hora", "red"),
    Verdict.NOT_OFFERED: ("Descartada: el día no ofrece esta hora", "red"),
    Verdict.ALREADY_BOOKED: ("Descartada: ya tengo clase a esta hora", "red"),
}

_OUTCOME_TEXT: dict[Outcome, str] = {
    Outcome.READY: "Hay intentos de reserva",
    Outcome.NO_PENDING_TOPICS: "No reserva nada: no hay temas pendientes",
    Outcome.NO_DAYS: "No reserva nada: la API no devolvió días",
    Outcome.WEEKDAY_EXCLUDED: "No reserva nada: el día objetivo no está en TARGET_WEEKDAYS",
    Outcome.DAILY_CAP_REACHED: "No reserva nada: ya se alcanzó el tope de clases de ese día",
    Outcome.NO_CANDIDATES: "No reserva nada: ninguna hora preferida es reservable",
}

_TARGET_TEXT: dict[TargetReason, str] = {
    TargetReason.NEW_DAY: "día nuevo respecto a la foto previa a la apertura",
    TargetReason.FURTHEST_DAY: "día más lejano visible, sin día nuevo que comparar",
}


def _hhmm(hora: dt.time) -> str:
    return hora.strftime("%H:%M")


def _fecha(fecha: dt.date) -> str:
    return f"{_WEEKDAYS[fecha.weekday()]} {fecha.isoformat()}"


def plan_summary(plan: Plan) -> Text:
    """A few lines saying what will be attempted, in which order, and why."""
    lines = Text()
    if plan.target_date is not None and plan.target_reason is not None:
        lines.append("Fecha objetivo: ", style="bold")
        lines.append(f"{_fecha(plan.target_date)} ({_TARGET_TEXT[plan.target_reason]})\n")
    lines.append("Resultado: ", style="bold")
    lines.append(_OUTCOME_TEXT[plan.outcome], style="green" if plan.intentos else "red")
    if plan.intentos:
        lines.append("\nOrden de intentos: ", style="bold")
        steps = []
        for intento in plan.intentos:
            suffix = " (segunda opción)" if intento.kind is AttemptKind.SECOND_OPTION else ""
            steps.append(f"{_hhmm(intento.hora)}{suffix}")
        lines.append(" -> ".join(steps))
        lines.append("\nReservas que aún caben ese día: ", style="bold")
        lines.append(str(plan.remaining))
    return lines


def plan_table(plan: Plan, day: Dia | None) -> Table:
    """Every block of the target day, with the planner's verdict on the preferred ones."""
    table = Table(expand=True)
    table.add_column("Hora")
    table.add_column("Cupos", justify="right")
    table.add_column("Preferencia", justify="center")
    table.add_column("Decisión")

    evaluations = {e.hora: e for e in plan.evaluations}
    offered = sorted(day.horas, key=lambda h: h.hora) if day is not None else []

    for slot in offered:
        evaluation = evaluations.get(slot.hora)
        if evaluation is None:
            table.add_row(
                _hhmm(slot.hora), str(slot.disponible), "-", "No está en TARGET_HOURS", style="dim"
            )
        else:
            text, style = _VERDICT_TEXT[evaluation.verdict]
            table.add_row(
                _hhmm(slot.hora),
                str(slot.disponible),
                f"#{evaluation.priority}",
                Text(text, style=style),
            )

    offered_hours = {slot.hora for slot in offered}
    for evaluation in plan.evaluations:
        if evaluation.hora not in offered_hours:
            text, style = _VERDICT_TEXT[evaluation.verdict]
            table.add_row(
                _hhmm(evaluation.hora), "-", f"#{evaluation.priority}", Text(text, style=style)
            )
    return table


def plan_panel(title: str, plan: Plan, day: Dia | None) -> RenderableType:
    """Summary plus table, framed: what the bot would book and why."""
    body: list[RenderableType] = [plan_summary(plan)]
    if plan.evaluations:
        body.append(plan_table(plan, day))
    return Panel(Group(*body), title=title, title_align="left")
