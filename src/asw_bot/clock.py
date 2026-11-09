"""Clocks: the only place the bot reads the time or waits.

The runner never calls ``datetime.now()`` or ``asyncio.sleep()`` itself; it is
handed a ``Clock``. That makes the whole opening window testable: a test can run
at real pace from a chosen instant (``ShiftedClock``) or in virtual time that
only moves when someone sleeps (``ManualClock``).

Server offset estimation and the high-precision wait arrive in phase 5.
"""

import asyncio
import datetime as dt
import time
from typing import Protocol


class Clock(Protocol):
    def now(self) -> dt.datetime:
        """Current instant, always timezone-aware."""
        ...

    async def sleep_until(self, when: dt.datetime) -> None:
        """Return at ``when`` or later, never earlier."""
        ...


class SystemClock:
    """The real clock."""

    def now(self) -> dt.datetime:
        return dt.datetime.now(dt.UTC)

    async def sleep_until(self, when: dt.datetime) -> None:
        # Loop: the event loop may wake a timer slightly early (its resolution
        # is about 15 ms on Windows), and "never earlier" is what keeps the
        # polling pace honest.
        while (remaining := (when - self.now()).total_seconds()) > 0:
            await asyncio.sleep(remaining)


class ShiftedClock(SystemClock):
    """Real pace, chosen origin: ``now()`` starts at ``start`` and advances normally."""

    def __init__(self, start: dt.datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("start must be timezone-aware")
        self._start = start
        self._origin = time.perf_counter()

    def now(self) -> dt.datetime:
        return self._start + dt.timedelta(seconds=time.perf_counter() - self._origin)


class ManualClock:
    """Virtual time: it moves only when someone sleeps or calls ``advance``."""

    def __init__(self, start: dt.datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("start must be timezone-aware")
        self._now = start

    def now(self) -> dt.datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += dt.timedelta(seconds=seconds)

    async def sleep_until(self, when: dt.datetime) -> None:
        self._now = max(self._now, when)
        await asyncio.sleep(0)  # still yield, as a real sleep would
