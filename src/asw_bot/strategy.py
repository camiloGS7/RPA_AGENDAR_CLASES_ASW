"""Pure booking logic: what to book given an availability snapshot.

Nothing in this module does I/O, reads the clock or touches the environment.
Everything it needs arrives as arguments (including "now"), and everything it
decides leaves as a return value. Same inputs, same plan, every time.

The plan answers one question: *which booking should be attempted next, and
what are the fallbacks?* ``Plan.intentos`` is that ordered list of candidates.
After a successful booking the caller asks again with fresh data: the topic
has advanced, and the hour just booked is now excluded.
"""

import datetime as dt
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from enum import StrEnum

from asw_bot.config import ASW_MAX_CLASSES_PER_DAY, Settings
from asw_bot.models import Disponibilidad, Reserva

# ASW rule: a block can be scheduled up to one hour before it starts.
MIN_LEAD_TIME = dt.timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class Preferences:
    """The user's booking preferences, detached from where they are configured."""

    target_hours: tuple[dt.time, ...]
    target_weekdays: frozenset[int]
    max_classes_per_day: int = 1
    allow_second_option: bool = False

    @classmethod
    def from_settings(cls, settings: Settings) -> "Preferences":
        return cls(
            target_hours=settings.target_hours,
            target_weekdays=frozenset(settings.target_weekdays),
            max_classes_per_day=settings.max_classes_per_day,
            allow_second_option=settings.allow_second_option,
        )


class AttemptKind(StrEnum):
    NORMAL = "normal"
    # Same hour on the next working day, offered only when the block is full.
    SECOND_OPTION = "segunda_opcion"


@dataclass(frozen=True, slots=True)
class Intento:
    """One booking the runner may try.

    For a SECOND_OPTION attempt, ``fecha`` is the full day that was asked for;
    the real date comes from ``ver_mas_opciones`` at execution time.
    """

    kind: AttemptKind
    fecha: dt.date
    hora: dt.time
    tema_id: int
    tipo_clase: str | None
    seats_seen: int
    priority: int


class Verdict(StrEnum):
    """What the planner concluded about one preferred hour."""

    BOOK = "book"
    SECOND_OPTION = "second_option"
    FULL = "full"
    CLOSED = "closed"
    TOO_LATE = "too_late"
    NOT_OFFERED = "not_offered"
    ALREADY_BOOKED = "already_booked"


@dataclass(frozen=True, slots=True)
class HourEvaluation:
    hora: dt.time
    priority: int
    verdict: Verdict
    seats: int | None


class Outcome(StrEnum):
    """Why the plan has, or does not have, attempts."""

    READY = "ready"
    NO_PENDING_TOPICS = "no_pending_topics"
    NO_DAYS = "no_days"
    WEEKDAY_EXCLUDED = "weekday_excluded"
    DAILY_CAP_REACHED = "daily_cap_reached"
    NO_CANDIDATES = "no_candidates"


class TargetReason(StrEnum):
    NEW_DAY = "new_day"
    FURTHEST_DAY = "furthest_day"


@dataclass(frozen=True, slots=True)
class Plan:
    """The decision plus the reasoning behind it, so it can be logged and shown."""

    outcome: Outcome
    target_date: dt.date | None = None
    target_reason: TargetReason | None = None
    remaining: int = 0
    intentos: tuple[Intento, ...] = ()
    evaluations: tuple[HourEvaluation, ...] = ()


def pick_target_date(
    fechas: Collection[dt.date], fechas_previas: Collection[dt.date] | None
) -> tuple[dt.date, TargetReason]:
    """Choose the day to book: the one that just appeared, else the furthest one.

    ``fechas_previas`` is the set of days visible before the window opened.
    Days are compared as a set because the API does not return them in order.
    """
    if fechas_previas is not None:
        new_days = set(fechas) - set(fechas_previas)
        if new_days:
            return max(new_days), TargetReason.NEW_DAY
    return max(fechas), TargetReason.FURTHEST_DAY


def build_plan(
    disponibilidad: Disponibilidad | None,
    preferencias: Preferences,
    reservas_existentes: Sequence[Reserva],
    ahora: dt.datetime,
    fechas_previas: Collection[dt.date] | None = None,
) -> Plan:
    """Decide what to book next and record why.

    ``disponibilidad`` is None when the API reported no pending topics.
    ``ahora`` must be timezone-aware: the one-hour rule compares instants.
    """
    if ahora.tzinfo is None:
        raise ValueError("ahora must be timezone-aware")
    if disponibilidad is None:
        return Plan(Outcome.NO_PENDING_TOPICS)
    if not disponibilidad.horarios:
        return Plan(Outcome.NO_DAYS)

    target, reason = pick_target_date(disponibilidad.fechas, fechas_previas)
    day = disponibilidad.day(target)
    assert day is not None  # noqa: S101 - target comes from disponibilidad.fechas

    if day.weekday not in preferencias.target_weekdays:
        return Plan(Outcome.WEEKDAY_EXCLUDED, target, reason)

    booked = [r for r in reservas_existentes if r.booked_date == target]
    cap = min(preferencias.max_classes_per_day, ASW_MAX_CLASSES_PER_DAY)
    remaining = cap - len(booked)
    if remaining <= 0:
        return Plan(Outcome.DAILY_CAP_REACHED, target, reason)
    booked_times = {r.booked_time for r in booked}

    normal: list[Intento] = []
    second: list[Intento] = []
    evaluations: list[HourEvaluation] = []

    # dict.fromkeys: drop repeated hours while keeping the priority order.
    for priority, hora in enumerate(dict.fromkeys(preferencias.target_hours), start=1):
        slot = day.slot(hora)
        seats = slot.disponible if slot is not None else None
        kind: AttemptKind | None = None

        if slot is None:
            verdict = Verdict.NOT_OFFERED
        elif hora in booked_times:
            verdict = Verdict.ALREADY_BOOKED
        elif day.starts_at(hora) - ahora < MIN_LEAD_TIME:
            verdict = Verdict.TOO_LATE
        elif slot.mensaje_cierre:
            verdict = Verdict.CLOSED
        elif slot.disponible > 0:
            verdict, kind = Verdict.BOOK, AttemptKind.NORMAL
        elif preferencias.allow_second_option:
            verdict, kind = Verdict.SECOND_OPTION, AttemptKind.SECOND_OPTION
        else:
            verdict = Verdict.FULL

        evaluations.append(HourEvaluation(hora, priority, verdict, seats))
        if kind is not None:
            intento = Intento(
                kind=kind,
                fecha=target,
                hora=hora,
                tema_id=disponibilidad.tema.id,
                tipo_clase=disponibilidad.tema.tipo_clase,
                seats_seen=seats or 0,
                priority=priority,
            )
            (normal if kind is AttemptKind.NORMAL else second).append(intento)

    # A seat on the target day always beats moving a class to another day.
    intentos = (*normal, *second)
    return Plan(
        outcome=Outcome.READY if intentos else Outcome.NO_CANDIDATES,
        target_date=target,
        target_reason=reason,
        remaining=remaining,
        intentos=intentos,
        evaluations=tuple(evaluations),
    )


def plan_bookings(
    disponibilidad: Disponibilidad | None,
    preferencias: Preferences,
    reservas_existentes: Sequence[Reserva],
    ahora: dt.datetime,
    fechas_previas: Collection[dt.date] | None = None,
) -> list[Intento]:
    """Ordered candidates for the next booking; empty when nothing should be booked."""
    plan = build_plan(disponibilidad, preferencias, reservas_existentes, ahora, fechas_previas)
    return list(plan.intentos)
