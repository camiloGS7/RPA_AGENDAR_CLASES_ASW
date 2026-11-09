"""End to end over real sockets and real time: the mock runs under uvicorn on a local port.

Each scenario takes a few seconds because the bot really waits for the opening,
so every scenario runs once per module and several tests read its outcome.
"""

import asyncio
import datetime as dt
from dataclasses import dataclass
from typing import Any

import pytest
from mock_server.app import LoggedRequest, MockAsw, MockConfig, MockServer

from asw_bot.client import AswClient
from asw_bot.clock import ShiftedClock
from asw_bot.models import BOGOTA
from asw_bot.runner import RunResult, RunStatus, WindowRunner
from asw_bot.token_store import MemoryTokenStore
from helpers import make_settings

T = dt.datetime(2026, 11, 10, 4, 40, tzinfo=BOGOTA)  # Tuesday; the new day is Wednesday
WED = dt.date(2026, 11, 11)
POLL = dt.timedelta(milliseconds=400)
# Slack for scheduling jitter of a loaded machine; the pacing itself has none.
JITTER = dt.timedelta(milliseconds=150)


@dataclass
class Live:
    result: RunResult
    state: MockAsw

    def at_server(self, endpoint: str) -> list[LoggedRequest]:
        return [r for r in self.state.requests if r.endpoint == endpoint]


def run_live(*, lead_s: float, config: dict[str, Any], settings: dict[str, Any]) -> Live:
    """Start the mock, point the bot at it and run one whole window."""
    clocks: list[ShiftedClock] = []
    mock_config = MockConfig(open_at=T, clock=lambda: clocks[0].now(), **config)

    async def scenario(base_url: str) -> RunResult:
        run_settings = make_settings(asw_base_url=base_url, **settings)
        async with AswClient(run_settings, MemoryTokenStore()) as client:
            return await WindowRunner(client, run_settings, clocks[0]).run()

    with MockServer(mock_config) as server:
        # The clock starts once the server is up, so startup time is not part of the lead.
        clocks.append(ShiftedClock(T - dt.timedelta(seconds=lead_s)))
        result = asyncio.run(scenario(server.base_url))
        return Live(result, server.state)


@pytest.fixture(scope="module")
def night() -> Live:
    """The normal night: default schedule, polling from T-3 s, competitors draining 07:30."""
    return run_live(
        lead_s=4.0,
        config={"drain_per_tick": {"07:30:00": 1}},
        settings={"target_hours": "07:30:00,09:00:00,10:30:00", "max_classes_per_day": 3},
    )


@pytest.fixture(scope="module")
def contested() -> Live:
    """The first preference is taken by someone else between seeing it and booking it."""
    return run_live(
        lead_s=1.6,
        config={"stolen_after_view": frozenset({"07:30:00"})},
        settings={
            "target_hours": "07:30:00,09:00:00,10:30:00,12:00:00",
            "max_classes_per_day": 3,
            "poll_start_offset_ms": -1000,
        },
    )


@pytest.fixture(scope="module")
def rehearsal() -> Live:
    """DRY_RUN=true against the same server."""
    return run_live(
        lead_s=1.6,
        config={},
        settings={"dry_run": True, "poll_start_offset_ms": -1000},
    )


# --- the normal night ---------------------------------------------------------


def test_books_the_first_preferences_and_verifies_them(night: Live) -> None:
    result = night.result

    assert result.status is RunStatus.BOOKED_ALL
    assert result.exit_code == 0
    assert [(b.fecha, f"{b.hora:%H:%M}") for b in result.booked] == [
        (WED, "07:30"),
        (WED, "09:00"),
        (WED, "10:30"),
    ]
    assert all(b.verified for b in result.booked)
    assert [b["tema_id"] for b in night.state.bookings] == [2563, 2564, 2565]


def test_opening_is_detected_within_one_poll_interval(night: Live) -> None:
    first_open = next(r for r in night.at_server("ver_disponibilidad") if r.status == 200)

    assert dt.timedelta(0) <= first_open.at - T < POLL + JITTER
    assert night.result.detected_at is not None
    assert night.result.detected_at - T < POLL + JITTER


def test_first_booking_lands_within_one_interval_plus_one_round_trip(night: Live) -> None:
    first_booking = night.at_server("agendar")[0]

    assert first_booking.status == 200
    assert first_booking.at - T < POLL + JITTER


def test_no_request_between_preparation_and_three_seconds_before_opening(night: Live) -> None:
    early = [r for r in night.state.requests if r.at < T - dt.timedelta(seconds=3)]

    assert [r.endpoint for r in early] == ["login", "opciones", "ver_disponibilidad", "clases"]
    # Preparation is a quick burst at the start, not a slow trickle towards the window.
    assert all(r.at < T - dt.timedelta(seconds=3.4) for r in early)


def test_polling_never_exceeds_one_request_per_400_ms(night: Live) -> None:
    window_start = T - dt.timedelta(seconds=3)
    sent = [
        r.started_at
        for r in night.result.requests
        if r.endpoint == "ver_disponibilidad" and window_start - JITTER <= r.started_at < T
    ]
    received = [
        r.at for r in night.at_server("ver_disponibilidad") if window_start - JITTER <= r.at < T
    ]

    assert len(sent) >= 7
    # As sent by the bot: the pace is exact.
    assert all(
        b - a >= POLL - dt.timedelta(milliseconds=5) for a, b in zip(sent, sent[1:], strict=False)
    )
    # As seen by the server: the same, give or take network jitter.
    assert all(
        b - a >= POLL - dt.timedelta(milliseconds=40)
        for a, b in zip(received, received[1:], strict=False)
    )
    assert all(r.status == 500 for r in night.at_server("ver_disponibilidad") if r.at < T)


def test_bookings_go_out_with_the_bogota_time_zone(night: Live) -> None:
    bodies = [r.body for r in night.at_server("agendar")]

    assert [b["hora"] for b in bodies] == ["07:30:00", "09:00:00", "10:30:00"]
    assert {b["zona_horaria"] for b in bodies} == {"America/Bogota"}
    assert {b["dia"] for b in bodies} == {"2026-11-11"}


def test_the_bot_saw_the_seats_drop_in_real_time(night: Live) -> None:
    seen = [o.seats[dt.time(7, 30)] for o in night.result.observations if o.fecha == WED]

    assert seen[0] <= 15
    assert seen == sorted(seen, reverse=True)
    assert seen[-1] < seen[0]


# --- a contested seat ----------------------------------------------------------


def test_switches_to_the_next_preference_when_the_seat_is_gone(contested: Live) -> None:
    result = contested.result

    assert (result.bookings[0].hora, result.bookings[0].ok) == (dt.time(7, 30), False)
    assert [f"{b.hora:%H:%M}" for b in result.booked] == ["09:00", "10:30", "12:00"]
    assert result.status is RunStatus.BOOKED_ALL
    assert [r.status for r in contested.at_server("agendar")] == [500, 200, 200, 200]


# --- dry run --------------------------------------------------------------------


def test_dry_run_never_reaches_agendar_or_cancelar(rehearsal: Live) -> None:
    assert rehearsal.result.status is RunStatus.DRY_RUN
    assert [f"{b.hora:%H:%M}" for b in rehearsal.result.booked] == ["07:30", "09:00", "10:30"]
    assert rehearsal.at_server("agendar") == []
    assert rehearsal.at_server("cancelar") == []
    assert rehearsal.state.bookings == []
