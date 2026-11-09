"""Clocks are aware, never wake early, and the manual one moves only on demand."""

import datetime as dt
import time

import pytest

from asw_bot.clock import ManualClock, ShiftedClock, SystemClock

START = dt.datetime(2026, 11, 10, 4, 40, tzinfo=dt.UTC)


def test_system_clock_is_timezone_aware() -> None:
    assert SystemClock().now().tzinfo is not None


async def test_system_clock_sleep_never_returns_early() -> None:
    clock = SystemClock()
    target = clock.now() + dt.timedelta(milliseconds=30)

    await clock.sleep_until(target)

    assert clock.now() >= target


async def test_sleeping_until_the_past_returns_at_once() -> None:
    clock = SystemClock()
    began = time.perf_counter()

    await clock.sleep_until(clock.now() - dt.timedelta(hours=1))

    assert time.perf_counter() - began < 0.05


async def test_shifted_clock_starts_at_the_given_instant_and_runs_at_real_pace() -> None:
    clock = ShiftedClock(START)
    first = clock.now()

    await clock.sleep_until(START + dt.timedelta(milliseconds=40))

    assert START <= first < START + dt.timedelta(milliseconds=30)
    assert START + dt.timedelta(milliseconds=40) <= clock.now() < START + dt.timedelta(seconds=1)


async def test_manual_clock_moves_only_when_told() -> None:
    clock = ManualClock(START)

    assert clock.now() == START
    await clock.sleep_until(START + dt.timedelta(minutes=5))
    assert clock.now() == START + dt.timedelta(minutes=5)
    await clock.sleep_until(START)  # the past: time does not go backwards
    assert clock.now() == START + dt.timedelta(minutes=5)
    clock.advance(1.5)
    assert clock.now() == START + dt.timedelta(minutes=5, seconds=1.5)


@pytest.mark.parametrize("factory", [ShiftedClock, ManualClock])
def test_naive_start_is_refused(factory: type) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        factory(dt.datetime(2026, 11, 10, 4, 40))  # noqa: DTZ001 - the point of the test
