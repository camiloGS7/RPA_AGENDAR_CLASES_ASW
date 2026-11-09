"""The planner is a pure function, so every rule is one row in a table of cases."""

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

import pytest

from asw_bot.models import BOGOTA, Disponibilidad, Reserva
from asw_bot.strategy import (
    AttemptKind,
    Outcome,
    Preferences,
    TargetReason,
    Verdict,
    build_plan,
    pick_target_date,
    plan_bookings,
)
from helpers import load_fixture, make_settings

MON = dt.date(2026, 11, 9)
TUE = dt.date(2026, 11, 10)
WED = dt.date(2026, 11, 11)
FRI = dt.date(2026, 11, 13)
SAT = dt.date(2026, 11, 14)

N = AttemptKind.NORMAL
S = AttemptKind.SECOND_OPTION

FREE = {"06:00:00": 18, "07:30:00": 15, "09:00:00": 24}
FIRST_FULL = {"06:00:00": 0, "07:30:00": 15, "09:00:00": 24}
ALL_FULL = {"06:00:00": 0, "07:30:00": 0, "09:00:00": 0}


def at(day: dt.date, hour: int, minute: int = 0, second: int = 0) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(hour, minute, second), tzinfo=BOGOTA)


# The moment the window opens on Tuesday: every block of the day is still far away.
OPENING = at(TUE, 4, 40)


def availability(days: dict[dt.date, dict[str, int]]) -> Disponibilidad:
    """Build a response with the days in the given (possibly unsorted) order."""
    return Disponibilidad.model_validate(
        {
            "tema": {"id": 2563, "tipo_clase": "regular"},
            "horarios": [
                {
                    "fecha": day.isoformat(),
                    "horas": [
                        {"id": i, "hora": hora, "disponible": seats}
                        for i, (hora, seats) in enumerate(hours.items(), start=1)
                    ],
                }
                for day, hours in days.items()
            ],
        }
    )


def bookings(day: dt.date, *hours: str) -> list[Reserva]:
    return [
        Reserva.model_validate({"id": i, "fecha": day.isoformat(), "hora": hora})
        for i, hora in enumerate(hours, start=1)
    ]


def hhmm(*values: str) -> tuple[dt.time, ...]:
    return tuple(dt.time.fromisoformat(v) for v in values)


@dataclass
class Case:
    days: dict[dt.date, dict[str, int]] | None
    outcome: Outcome
    expected: list[tuple[AttemptKind, dt.date, str]] = field(default_factory=list)
    hours: tuple[str, ...] = ("06:00", "07:30")
    weekdays: tuple[int, ...] = (1, 2, 3, 4, 5)
    max_per_day: int = 1
    second_option: bool = False
    reservas: list[Reserva] = field(default_factory=list)
    now: dt.datetime = OPENING
    previas: set[dt.date] | None = None

    def run(self) -> tuple[Any, list[tuple[AttemptKind, dt.date, str]]]:
        plan = build_plan(
            availability(self.days) if self.days is not None else None,
            Preferences(
                target_hours=hhmm(*self.hours),
                target_weekdays=frozenset(self.weekdays),
                max_classes_per_day=self.max_per_day,
                allow_second_option=self.second_option,
            ),
            self.reservas,
            self.now,
            self.previas,
        )
        return plan, [(i.kind, i.fecha, i.hora.strftime("%H:%M")) for i in plan.intentos]


CASES = [
    # --- seats and priority ---
    pytest.param(
        Case({TUE: FREE}, Outcome.READY, [(N, TUE, "06:00"), (N, TUE, "07:30")]),
        id="first-preference-free-next-is-the-fallback",
    ),
    pytest.param(
        Case({TUE: FIRST_FULL}, Outcome.READY, [(N, TUE, "07:30")]),
        id="first-preference-full-takes-the-second",
    ),
    pytest.param(Case({TUE: ALL_FULL}, Outcome.NO_CANDIDATES), id="everything-full"),
    pytest.param(
        Case(
            {TUE: FREE},
            Outcome.READY,
            [(N, TUE, "07:30"), (N, TUE, "06:00")],
            hours=("07:30", "06:00"),
        ),
        id="order-follows-target-hours-not-the-clock",
    ),
    pytest.param(
        Case(
            {TUE: FREE},
            Outcome.READY,
            [(N, TUE, "06:00"), (N, TUE, "07:30")],
            hours=("06:00", "06:00", "07:30"),
        ),
        id="repeated-hours-are-planned-once",
    ),
    pytest.param(
        Case({TUE: {"07:30:00": 15}}, Outcome.READY, [(N, TUE, "07:30")]),
        id="hour-the-day-does-not-offer-is-skipped",
    ),
    # --- second option ---
    pytest.param(
        Case(
            {TUE: ALL_FULL},
            Outcome.READY,
            [(S, TUE, "06:00"), (S, TUE, "07:30")],
            second_option=True,
        ),
        id="everything-full-with-second-option-enabled",
    ),
    pytest.param(
        Case(
            {TUE: FIRST_FULL},
            Outcome.READY,
            [(N, TUE, "07:30"), (S, TUE, "06:00")],
            second_option=True,
        ),
        id="second-option-comes-after-seats-on-the-target-day",
    ),
    # --- weekdays ---
    pytest.param(
        Case({FRI: FREE, SAT: FREE}, Outcome.WEEKDAY_EXCLUDED),
        id="saturday-excluded",
    ),
    pytest.param(
        Case(
            {FRI: FREE, SAT: FREE},
            Outcome.READY,
            [(N, SAT, "06:00"), (N, SAT, "07:30")],
            weekdays=(1, 2, 3, 4, 5, 6),
        ),
        id="saturday-allowed-when-configured",
    ),
    # --- daily cap ---
    pytest.param(
        Case(
            {TUE: FREE},
            Outcome.DAILY_CAP_REACHED,
            max_per_day=5,
            reservas=bookings(TUE, "09:00:00", "10:30:00", "12:00:00", "13:30:00", "15:00:00"),
        ),
        id="asw-cap-of-five-reached",
    ),
    pytest.param(
        Case(
            {TUE: FREE},
            Outcome.DAILY_CAP_REACHED,
            max_per_day=9,
            reservas=bookings(TUE, "09:00:00", "10:30:00", "12:00:00", "13:30:00", "15:00:00"),
        ),
        id="never-more-than-five-even-if-configured-higher",
    ),
    pytest.param(
        Case({TUE: FREE}, Outcome.DAILY_CAP_REACHED, reservas=bookings(TUE, "18:00:00")),
        id="default-cap-of-one-already-used",
    ),
    pytest.param(
        Case(
            {MON: FREE, TUE: FREE},
            Outcome.READY,
            [(N, TUE, "06:00"), (N, TUE, "07:30")],
            reservas=bookings(MON, "18:00:00"),
        ),
        id="bookings-on-other-days-do-not-count",
    ),
    pytest.param(
        Case(
            {TUE: FREE},
            Outcome.READY,
            [(N, TUE, "07:30")],
            max_per_day=2,
            reservas=bookings(TUE, "06:00:00"),
        ),
        id="second-class-skips-the-hour-already-booked",
    ),
    # --- one-hour rule ---
    pytest.param(
        Case({TUE: FREE}, Outcome.READY, [(N, TUE, "07:30")], now=at(TUE, 5, 30)),
        id="less-than-one-hour-before-is-too-late",
    ),
    pytest.param(
        Case(
            {TUE: FREE},
            Outcome.READY,
            [(N, TUE, "06:00"), (N, TUE, "07:30")],
            now=at(TUE, 5, 0),
        ),
        id="exactly-one-hour-before-is-allowed",
    ),
    pytest.param(
        Case({TUE: FREE}, Outcome.READY, [(N, TUE, "07:30")], now=at(TUE, 5, 0, 1)),
        id="one-second-inside-the-hour-is-too-late",
    ),
    pytest.param(
        Case({TUE: FREE}, Outcome.NO_CANDIDATES, now=at(TUE, 12)),
        id="every-preferred-hour-already-passed",
    ),
    # --- target day ---
    pytest.param(
        Case(
            {WED: FREE, MON: FREE, TUE: FREE}, Outcome.READY, [(N, WED, "06:00"), (N, WED, "07:30")]
        ),
        id="unordered-days-furthest-day-wins",
    ),
    pytest.param(
        Case(
            {TUE: FREE, WED: FREE},
            Outcome.READY,
            [(N, WED, "06:00"), (N, WED, "07:30")],
            previas={MON, TUE},
        ),
        id="new-day-detected",
    ),
    pytest.param(
        Case(
            {WED: FREE, TUE: FREE},
            Outcome.READY,
            [(N, WED, "06:00"), (N, WED, "07:30")],
            previas={MON, TUE},
        ),
        id="new-day-detected-when-listed-first",
    ),
    pytest.param(
        Case(
            {MON: FREE, TUE: FREE, WED: FREE},
            Outcome.READY,
            [(N, TUE, "06:00"), (N, TUE, "07:30")],
            previas={MON, WED},
        ),
        id="new-day-beats-a-later-day-that-was-already-visible",
    ),
    pytest.param(
        Case(
            {MON: FREE, TUE: FREE},
            Outcome.READY,
            [(N, TUE, "06:00"), (N, TUE, "07:30")],
            previas={MON, TUE},
        ),
        id="no-new-day-falls-back-to-the-furthest",
    ),
    # --- nothing to plan ---
    pytest.param(Case(None, Outcome.NO_PENDING_TOPICS), id="no-pending-topics"),
    pytest.param(Case({}, Outcome.NO_DAYS), id="api-returned-no-days"),
]


@pytest.mark.parametrize("case", CASES)
def test_plan(case: Case) -> None:
    plan, attempts = case.run()

    assert attempts == case.expected
    assert plan.outcome is case.outcome


def test_planning_is_deterministic_and_matches_plan_bookings() -> None:
    args = (
        availability({TUE: FIRST_FULL}),
        Preferences(hhmm("06:00", "07:30"), frozenset({2})),
        [],
        OPENING,
    )

    first, second = build_plan(*args), build_plan(*args)

    assert first == second
    assert plan_bookings(*args) == list(first.intentos)


def test_attempt_carries_what_agendar_needs() -> None:
    plan = build_plan(
        availability({TUE: FREE}), Preferences(hhmm("07:30"), frozenset({2})), [], OPENING
    )

    (intento,) = plan.intentos
    assert intento.kind is AttemptKind.NORMAL
    assert (intento.fecha, intento.hora) == (TUE, dt.time(7, 30))
    assert intento.tema_id == 2563
    assert intento.tipo_clase == "regular"
    assert intento.seats_seen == 15
    assert intento.priority == 1
    assert plan.remaining == 1


def test_plan_explains_every_preferred_hour() -> None:
    disponibilidad = Disponibilidad.model_validate(
        {
            "tema": {"id": 1},
            "horarios": [
                {
                    "fecha": TUE.isoformat(),
                    "horas": [
                        {"id": 1, "hora": "06:00:00", "disponible": 0},
                        {"id": 2, "hora": "07:30:00", "disponible": 9, "mensaje_cierre": "Cerrada"},
                        {"id": 3, "hora": "09:00:00", "disponible": 4},
                        {"id": 4, "hora": "10:30:00", "disponible": 4},
                        {"id": 5, "hora": "12:00:00", "disponible": 4},
                    ],
                }
            ],
        }
    )
    preferences = Preferences(
        hhmm("06:00", "07:30", "09:00", "10:30", "12:00", "19:30"),
        frozenset({2}),
        max_classes_per_day=3,
    )

    plan = build_plan(disponibilidad, preferences, bookings(TUE, "09:00:00"), at(TUE, 9, 45))

    assert [(e.hora.strftime("%H:%M"), e.verdict) for e in plan.evaluations] == [
        ("06:00", Verdict.TOO_LATE),
        ("07:30", Verdict.TOO_LATE),
        ("09:00", Verdict.ALREADY_BOOKED),
        ("10:30", Verdict.TOO_LATE),
        ("12:00", Verdict.BOOK),
        ("19:30", Verdict.NOT_OFFERED),
    ]
    assert [e.priority for e in plan.evaluations] == [1, 2, 3, 4, 5, 6]
    assert plan.remaining == 2


def test_closed_and_full_blocks_are_told_apart() -> None:
    disponibilidad = Disponibilidad.model_validate(
        {
            "tema": {"id": 1},
            "horarios": [
                {
                    "fecha": TUE.isoformat(),
                    "horas": [
                        {"id": 1, "hora": "06:00:00", "disponible": 9, "mensaje_cierre": "Cerrada"},
                        {"id": 2, "hora": "07:30:00", "disponible": 0},
                    ],
                }
            ],
        }
    )

    plan = build_plan(
        disponibilidad,
        Preferences(hhmm("06:00", "07:30"), frozenset({2}), allow_second_option=True),
        [],
        OPENING,
    )

    # A closed block is never offered as second option; a full one is.
    assert [e.verdict for e in plan.evaluations] == [Verdict.CLOSED, Verdict.SECOND_OPTION]
    assert [(i.kind, i.hora) for i in plan.intentos] == [(S, dt.time(7, 30))]


@pytest.mark.parametrize(
    ("fechas", "previas", "expected"),
    [
        ({MON, TUE}, None, (TUE, TargetReason.FURTHEST_DAY)),
        ({TUE, WED}, {MON, TUE}, (WED, TargetReason.NEW_DAY)),
        ({MON, TUE}, {MON, TUE}, (TUE, TargetReason.FURTHEST_DAY)),
        ({TUE, WED}, set(), (WED, TargetReason.NEW_DAY)),
    ],
)
def test_target_date_reason(
    fechas: set[dt.date], previas: set[dt.date] | None, expected: tuple[dt.date, TargetReason]
) -> None:
    assert pick_target_date(fechas, previas) == expected


def test_now_without_timezone_is_refused() -> None:
    naive = dt.datetime(2026, 11, 10, 4, 40)  # noqa: DTZ001 - the point of the test

    with pytest.raises(ValueError, match="timezone-aware"):
        build_plan(availability({TUE: FREE}), Preferences(hhmm("06:00"), frozenset({2})), [], naive)


def test_now_in_another_zone_is_the_same_instant() -> None:
    # 10:30 UTC is 05:30 in Bogota: 06:00 is already inside the one-hour limit.
    utc_now = dt.datetime(2026, 11, 10, 10, 30, tzinfo=dt.UTC)

    attempts = plan_bookings(
        availability({TUE: FREE}), Preferences(hhmm("06:00", "07:30"), frozenset({2})), [], utc_now
    )

    assert [i.hora for i in attempts] == [dt.time(7, 30)]


def test_preferences_come_from_settings() -> None:
    preferences = Preferences.from_settings(
        make_settings(
            target_hours="18:00:00,06:00:00", target_weekdays="2,4", max_classes_per_day=3
        )
    )

    assert preferences == Preferences(
        target_hours=hhmm("18:00", "06:00"),
        target_weekdays=frozenset({2, 4}),
        max_classes_per_day=3,
        allow_second_option=False,
    )


# --- the availability documented in the spec ---------------------------------


def test_real_availability_books_tuesday_at_six() -> None:
    """Monday 13:52, default preferences: the furthest day is Tuesday, 06:00 has 18 seats."""
    disponibilidad = Disponibilidad.model_validate(load_fixture("ver_disponibilidad.json"))
    preferences = Preferences.from_settings(make_settings())

    plan = build_plan(disponibilidad, preferences, [], at(MON, 13, 52))

    assert plan.target_date == TUE
    assert plan.target_reason is TargetReason.FURTHEST_DAY
    assert [(i.hora, i.seats_seen) for i in plan.intentos] == [
        (dt.time(6, 0), 18),
        (dt.time(7, 30), 15),
    ]


def test_real_full_block_on_monday_falls_through_to_the_next_preference() -> None:
    """Monday 15:00 really had 0 seats; with only Monday visible the bot moves on."""
    payload = load_fixture("ver_disponibilidad.json")
    payload["horarios"] = [d for d in payload["horarios"] if d["fecha"] == MON.isoformat()]
    disponibilidad = Disponibilidad.model_validate(payload)

    plan = build_plan(
        disponibilidad, Preferences(hhmm("15:00", "16:30"), frozenset({1})), [], at(MON, 13, 52)
    )

    assert [e.verdict for e in plan.evaluations] == [Verdict.FULL, Verdict.BOOK]
    assert [i.hora for i in plan.intentos] == [dt.time(16, 30)]
