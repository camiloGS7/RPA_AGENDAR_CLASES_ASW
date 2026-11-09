"""The runner against the mock server, in-process and in virtual time.

No sockets and no real waiting: the mock is plugged into the client through an
ASGI transport, and both share a ManualClock that only moves when the runner
sleeps. A whole opening window runs in milliseconds and is fully deterministic.
"""

import datetime as dt
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from mock_server.app import Fault, MockAsw, MockConfig, create_app, sign_token

from asw_bot.client import AswClient
from asw_bot.clock import ManualClock
from asw_bot.errors import UnexpectedResponseError
from asw_bot.models import BOGOTA, Disponibilidad, Opcion
from asw_bot.runner import (
    RunResult,
    RunStatus,
    WindowRunner,
    find_opening,
    window_is_open,
)
from asw_bot.strategy import AttemptKind
from asw_bot.token_store import MemoryTokenStore
from helpers import make_settings

T = dt.datetime(2026, 11, 10, 4, 40, tzinfo=BOGOTA)  # Tuesday; the new day is Wednesday
TUE = dt.date(2026, 11, 10)
WED = dt.date(2026, 11, 11)
THU = dt.date(2026, 11, 12)
BASE_URL = "http://mock.test/api/student/"
ALL_FULL = dict.fromkeys(("07:30:00", "09:00:00", "10:30:00"), 0)


async def run_window(
    *,
    start: dt.datetime = T - dt.timedelta(seconds=5),
    config: dict[str, Any] | None = None,
    settings: dict[str, Any] | None = None,
    setup: Callable[[MockAsw], None] | None = None,
    token: str | None = None,
    mock_clock: Callable[[], dt.datetime] | None = None,
) -> tuple[RunResult, MockAsw]:
    clock = ManualClock(start)
    app = create_app(MockConfig(open_at=T, clock=mock_clock or clock.now, **(config or {})))
    state: MockAsw = app.state.mock
    if setup is not None:
        setup(state)
    run_settings = make_settings(asw_base_url=BASE_URL, **(settings or {}))
    async with AswClient(
        run_settings,
        MemoryTokenStore(token),
        retry_wait_s=0,
        transport=httpx.ASGITransport(app=app),
    ) as client:
        result = await WindowRunner(client, run_settings, clock).run()
    return result, state


def booked(result: RunResult) -> list[tuple[dt.date, str]]:
    return [(b.fecha, f"{b.hora:%H:%M}") for b in result.booked]


def endpoints(state: MockAsw) -> list[str]:
    return [r.endpoint for r in state.requests]


# --- the normal night ---------------------------------------------------------


async def test_books_the_three_classes_in_priority_order() -> None:
    result, state = await run_window()

    assert result.status is RunStatus.BOOKED_ALL
    assert result.exit_code == 0
    assert result.opening == T
    assert result.target_date == WED
    assert result.wanted == 3
    assert booked(result) == [(WED, "07:30"), (WED, "09:00"), (WED, "10:30")]
    assert all(b.verified for b in result.booked)
    # Each booking took the next topic, so the three classes are consecutive lessons.
    assert [b["tema_id"] for b in state.bookings] == [2563, 2564, 2565]


async def test_agendar_bodies_are_what_the_app_sends() -> None:
    _, state = await run_window()

    bodies = [r.body for r in state.requests if r.endpoint == "agendar"]

    assert bodies[0] == {
        "zona_horaria": "America/Bogota",
        "tema_id": None,
        "hora": "07:30:00",
        "dia": "2026-11-11",
        "tipo_clase": "regular",
        "tipo_solicitud": "normal",
    }
    assert [b["hora"] for b in bodies] == ["07:30:00", "09:00:00", "10:30:00"]


async def test_nothing_is_sent_between_preparation_and_three_seconds_before_opening() -> None:
    result, state = await run_window(start=T - dt.timedelta(minutes=15))
    polling_starts = T - dt.timedelta(seconds=3)

    early = [r for r in state.requests if r.at < polling_starts]

    assert [r.endpoint for r in early] == ["login", "opciones", "ver_disponibilidad", "clases"]
    assert all(r.at == T - dt.timedelta(minutes=15) for r in early)
    assert result.status is RunStatus.BOOKED_ALL


async def test_polls_are_paced_and_the_opening_is_detected_within_one_interval() -> None:
    result, state = await run_window()

    polls = [
        r
        for r in state.requests
        if r.endpoint == "ver_disponibilidad" and r.at >= T - dt.timedelta(seconds=3)
    ]
    before_opening = [r for r in polls if r.at < T]
    gaps = [
        (b.at - a.at).total_seconds()
        for a, b in zip(before_opening, before_opening[1:], strict=False)
    ]

    assert len(before_opening) == 8  # T-3.0 s ... T-0.2 s
    assert all(r.status == 500 for r in before_opening)
    assert all(gap >= 0.4 for gap in gaps)
    assert result.detected_at is not None
    assert dt.timedelta(0) <= result.detected_at - T < dt.timedelta(milliseconds=400)
    first_booking = next(r for r in state.requests if r.endpoint == "agendar")
    assert first_booking.at - T < dt.timedelta(milliseconds=400)


async def test_the_run_keeps_a_timeline_and_seat_observations() -> None:
    result, state = await run_window()

    assert len(result.requests) == len(state.requests)
    assert {r.endpoint for r in result.requests} >= {"opciones", "ver_disponibilidad", "agendar"}
    first, last = result.observations[0], result.observations[-1]
    assert first.fecha == WED
    assert first.seats == {dt.time(7, 30): 15, dt.time(9, 0): 24, dt.time(10, 30): 34}
    # After booking 07:30 and 09:00 the bot saw one seat fewer in each.
    assert last.seats == {dt.time(7, 30): 14, dt.time(9, 0): 23, dt.time(10, 30): 34}


# --- when things do not go to plan -------------------------------------------


async def test_falls_back_to_the_next_preference_when_a_seat_is_taken_first() -> None:
    result, state = await run_window(
        config={"stolen_after_view": frozenset({"07:30:00"})},
        settings={"target_hours": "07:30:00,09:00:00,10:30:00,12:00:00"},
    )

    lost = result.bookings[0]

    assert (lost.hora, lost.ok) == (dt.time(7, 30), False)
    assert lost.detail == "No hay cupos disponibles para este horario."
    assert booked(result) == [(WED, "09:00"), (WED, "10:30"), (WED, "12:00")]
    assert result.status is RunStatus.BOOKED_ALL
    # The refused block is tried once, never again.
    assert sum(1 for r in state.requests if r.body.get("hora") == "07:30:00") == 1


async def test_everything_full_books_nothing() -> None:
    result, state = await run_window(config={"new_day_seats": ALL_FULL})

    assert result.status is RunStatus.NOTHING_BOOKED
    assert result.exit_code == 1
    assert result.reason == "no_candidates"
    assert "agendar" not in endpoints(state)


async def test_partial_when_one_block_is_full() -> None:
    result, _ = await run_window(
        config={"new_day_seats": {"07:30:00": 0, "09:00:00": 5, "10:30:00": 5}}
    )

    assert booked(result) == [(WED, "09:00"), (WED, "10:30")]
    assert result.status is RunStatus.BOOKED_PARTIAL
    assert result.exit_code == 1


async def test_dry_run_plans_everything_and_sends_no_booking() -> None:
    result, state = await run_window(settings={"dry_run": True})

    assert result.status is RunStatus.DRY_RUN
    assert result.exit_code == 0
    assert booked(result) == [(WED, "07:30"), (WED, "09:00"), (WED, "10:30")]
    assert all(b.dry_run for b in result.bookings)
    assert "agendar" not in endpoints(state)
    assert "cancelar" not in endpoints(state)
    assert state.bookings == []


async def test_window_that_never_opens_times_out() -> None:
    frozen = T - dt.timedelta(hours=1)  # the server's own clock never reaches the opening

    result, state = await run_window(mock_clock=lambda: frozen)

    assert result.status is RunStatus.NOT_OPENED
    assert result.exit_code == 1
    assert "agendar" not in endpoints(state)
    polls = [r for r in result.requests if r.endpoint == "ver_disponibilidad"]
    assert polls[-1].started_at < T + dt.timedelta(seconds=60)
    assert len(polls) == 1 + 158  # snapshot + one poll every 400 ms from T-3 s to T+60 s


async def test_weekday_outside_the_preferences_books_nothing() -> None:
    result, state = await run_window(settings={"target_weekdays": "1,2,4,5"})  # no Wednesday

    assert result.status is RunStatus.NOTHING_TO_DO
    assert result.exit_code == 0
    assert result.reason == "weekday_excluded"
    assert "agendar" not in endpoints(state)


async def test_existing_bookings_count_towards_the_daily_cap() -> None:
    def already_one(state: MockAsw) -> None:
        state.bookings.append({"id": 1, "fecha": "2026-11-11", "hora": "18:00:00"})

    result, _ = await run_window(setup=already_one, settings={"max_classes_per_day": 2})

    assert result.wanted == 1
    assert booked(result) == [(WED, "07:30")]
    assert result.status is RunStatus.BOOKED_ALL


async def test_cap_already_reached_is_nothing_to_do() -> None:
    def already_one(state: MockAsw) -> None:
        state.bookings.append({"id": 1, "fecha": "2026-11-11", "hora": "18:00:00"})

    result, state = await run_window(setup=already_one, settings={"max_classes_per_day": 1})

    assert result.status is RunStatus.NOTHING_TO_DO
    assert result.reason == "daily_cap_reached"
    assert "agendar" not in endpoints(state)


# --- topics --------------------------------------------------------------------


async def test_no_pending_topics_ends_quietly() -> None:
    result, state = await run_window(config={"pending_topics": 0})

    assert result.status is RunStatus.NOTHING_TO_DO
    assert result.reason == "no_pending_topics"
    assert result.exit_code == 0
    assert "agendar" not in endpoints(state)


async def test_no_pending_topics_seen_in_the_snapshot_stops_before_waiting() -> None:
    result, state = await run_window(config={"pending_topics": 0, "locked_mode": "stale"})

    assert result.status is RunStatus.NOTHING_TO_DO
    assert endpoints(state) == ["login", "opciones", "ver_disponibilidad"]


async def test_topics_running_out_mid_window_keeps_what_was_booked() -> None:
    result, _ = await run_window(config={"pending_topics": 1})

    assert booked(result) == [(WED, "07:30")]
    assert result.status is RunStatus.BOOKED_PARTIAL


# --- lock variants ---------------------------------------------------------------


async def test_lock_that_answers_200_is_handled_by_spotting_the_new_day() -> None:
    result, state = await run_window(config={"locked_mode": "stale"})

    polls_before = [r for r in state.requests if r.endpoint == "ver_disponibilidad" and r.at < T]

    assert all(r.status == 200 for r in polls_before)
    # Seats were visible during the lock, yet nothing was attempted before the opening.
    assert all(r.at >= T for r in state.requests if r.endpoint == "agendar")
    assert result.status is RunStatus.BOOKED_ALL
    assert result.target_date == WED


async def test_started_after_the_opening_it_books_straight_away() -> None:
    result, state = await run_window(start=T + dt.timedelta(minutes=30))

    assert result.status is RunStatus.BOOKED_ALL
    assert booked(result) == [(WED, "07:30"), (WED, "09:00"), (WED, "10:30")]
    assert endpoints(state)[:5] == [
        "login",
        "opciones",
        "ver_disponibilidad",
        "clases",
        "ver_disponibilidad",
    ]


@pytest.mark.parametrize(
    ("days", "previas", "seconds_from_opening", "expected"),
    [
        # No snapshot (the lock answered with an error): a 200 near T means open.
        ({TUE, WED}, None, 0.2, True),
        ({TUE}, None, -0.5, True),
        ({TUE}, None, -2.6, False),
        # Snapshot available: a new day is the signal, at any time.
        ({TUE, WED}, {TUE}, -2.6, True),
        ({TUE}, {TUE}, 0.2, False),
        # Same days long after T: trust the published time.
        ({TUE}, {TUE}, 1.0, True),
    ],
)
def test_window_is_open(
    days: set[dt.date], previas: set[dt.date] | None, seconds_from_opening: float, expected: bool
) -> None:
    disponibilidad = Disponibilidad.model_validate(
        {"tema": {"id": 1}, "horarios": [{"fecha": d.isoformat()} for d in days]}
    )
    now = T + dt.timedelta(seconds=seconds_from_opening)

    assert window_is_open(disponibilidad, previas, now, T) is expected


# --- failures --------------------------------------------------------------------


async def test_lost_response_is_confirmed_with_clases_instead_of_booking_twice() -> None:
    def lose_first_response(state: MockAsw) -> None:
        state.faults.append(Fault("agendar", status=502, process=True))

    result, state = await run_window(setup=lose_first_response)

    first = result.bookings[0]

    assert first.ok
    assert "clases shows it booked: True" in (first.detail or "")
    assert booked(result) == [(WED, "07:30"), (WED, "09:00"), (WED, "10:30")]
    # Three classes on the server, not four: the 07:30 request was never repeated.
    assert [b["hora"] for b in state.bookings] == ["07:30:00", "09:00:00", "10:30:00"]
    assert sum(1 for r in state.requests if r.endpoint == "agendar") == 3


async def test_dropped_request_moves_on_to_the_next_preference() -> None:
    def drop_first_request(state: MockAsw) -> None:
        state.faults.append(Fault("agendar", status=502, process=False))

    result, state = await run_window(setup=drop_first_request)

    assert (result.bookings[0].hora, result.bookings[0].ok) == (dt.time(7, 30), False)
    assert booked(result) == [(WED, "09:00"), (WED, "10:30")]
    assert result.status is RunStatus.BOOKED_PARTIAL
    assert [b["hora"] for b in state.bookings] == ["09:00:00", "10:30:00"]


async def test_failed_polls_are_just_followed_by_the_next_poll() -> None:
    def break_four(state: MockAsw) -> None:
        # Three hit the snapshot (one try + two retries), the fourth hits a poll.
        state.faults += [Fault("ver_disponibilidad")] * 4

    result, state = await run_window(setup=break_four)

    polls = [r for r in state.requests if r.endpoint == "ver_disponibilidad"]

    assert [r.status for r in polls[:4]] == [502, 502, 502, 502]
    assert result.status is RunStatus.BOOKED_ALL


async def test_expired_token_is_replaced_and_the_run_continues() -> None:
    old = sign_token({"uid": 1, "data": {"gen": 0}, "iat": 0})

    result, state = await run_window(token=old)

    assert endpoints(state)[:3] == ["opciones", "login", "opciones"]
    assert [r.status for r in state.requests[:3]] == [401, 200, 200]
    assert result.status is RunStatus.BOOKED_ALL


async def test_login_failure_fails_the_run() -> None:
    result, state = await run_window(config={"password": "the-right-one"})

    assert result.status is RunStatus.FAILED
    assert result.exit_code == 1
    assert result.reason is not None
    assert result.reason.startswith("AuthError")
    assert endpoints(state) == ["login"]


def test_opening_comes_from_the_clase_option() -> None:
    def option(value: str, hour: int) -> Opcion:
        stamp = f"2026-11-10T{hour:02d}:40:00"
        return Opcion(text=value, value=value, min_date=stamp, max_date=stamp)  # type: ignore[arg-type]

    assert find_opening([option("tutoria", 5), option("clase", 4)]) == T
    with pytest.raises(UnexpectedResponseError, match="clase"):
        find_opening([option("tutoria", 5)])


# --- second option ---------------------------------------------------------------


async def test_second_option_books_the_same_hour_on_the_next_working_day() -> None:
    result, state = await run_window(
        config={"new_day_seats": {"07:30:00": 0}},
        settings={
            "target_hours": "07:30:00",
            "max_classes_per_day": 1,
            "allow_second_option": True,
        },
    )

    (record,) = result.bookings

    assert record.kind is AttemptKind.SECOND_OPTION
    assert (record.fecha, record.hora, record.ok) == (THU, dt.time(7, 30), True)
    assert record.verified is True
    assert result.status is RunStatus.BOOKED_ALL
    body = next(r.body for r in state.requests if r.endpoint == "agendar")
    assert body == {
        "tema_id": 2563,
        "hora": "07:30:00",
        "dia": "2026-11-12",
        "tipo_clase": None,
        "tipo_solicitud": "normal",
        "segunda_opcion": True,
    }


async def test_second_option_on_an_unwanted_weekday_is_declined() -> None:
    result, state = await run_window(
        config={"new_day_seats": {"07:30:00": 0}},
        settings={
            "target_hours": "07:30:00",
            "target_weekdays": "1,2,3,5",  # Wednesday yes, Thursday no
            "allow_second_option": True,
        },
    )

    assert result.bookings[0].ok is False
    assert result.bookings[0].detail == "second option does not fit the preferences"
    assert result.status is RunStatus.NOTHING_BOOKED
    assert "agendar" not in endpoints(state)
