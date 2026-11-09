"""Orchestration of the opening window: prepare, wait, poll, book, verify (spec section 8).

This is the imperative shell around the pure strategy. It decides nothing about
*what* to book; it asks ``build_plan`` and carries the answer out, dealing with
time, the network and partial failures on the way.
"""

import datetime as dt
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

import structlog
from structlog.typing import FilteringBoundLogger

from asw_bot.client import AswClient, RequestRecord
from asw_bot.clock import Clock
from asw_bot.config import Settings
from asw_bot.errors import (
    AswError,
    BusinessError,
    BusinessErrorKind,
    NetworkError,
    ServerError,
    UnexpectedResponseError,
)
from asw_bot.models import Disponibilidad, Opcion, Reserva
from asw_bot.strategy import (
    AttemptKind,
    Intento,
    Outcome,
    Preferences,
    Verdict,
    accept_second_option,
    build_plan,
    pick_target_date,
)

log: FilteringBoundLogger = structlog.get_logger(__name__)

# If the lock keeps answering 200 with the same days, trust the published
# opening time once this long has passed.
STALE_LOCK_GRACE = dt.timedelta(seconds=1)
# Without a pre-opening snapshot, do not believe a 200 that arrives well before
# the opening time: it may be a lock that answers normally.
CLOCK_TOLERANCE = dt.timedelta(seconds=1)

_TRANSIENT = (ServerError, NetworkError, UnexpectedResponseError)


class RunStatus(StrEnum):
    BOOKED_ALL = "booked_all"
    BOOKED_PARTIAL = "booked_partial"
    NOTHING_BOOKED = "nothing_booked"
    NOTHING_TO_DO = "nothing_to_do"
    NOT_OPENED = "not_opened"
    DRY_RUN = "dry_run"
    FAILED = "failed"


_OK_STATUSES = frozenset({RunStatus.BOOKED_ALL, RunStatus.NOTHING_TO_DO, RunStatus.DRY_RUN})


@dataclass(frozen=True, slots=True)
class RequestEvent:
    """One HTTP call placed on the run's timeline."""

    endpoint: str
    started_at: dt.datetime
    rtt_ms: float
    status: int | None
    error: str | None


@dataclass(frozen=True, slots=True)
class SeatObservation:
    """Seats of the preferred hours on the target day, as seen in one response."""

    at: dt.datetime
    fecha: dt.date
    seats: dict[dt.time, int]


@dataclass(slots=True)
class BookingRecord:
    kind: AttemptKind
    fecha: dt.date
    hora: dt.time
    ok: bool
    at: dt.datetime
    detail: str | None = None
    dry_run: bool = False
    verified: bool | None = None
    stop: bool = False


@dataclass(slots=True)
class RunResult:
    status: RunStatus = RunStatus.FAILED
    reason: str | None = None
    opening: dt.datetime | None = None
    detected_at: dt.datetime | None = None
    target_date: dt.date | None = None
    wanted: int = 0
    bookings: list[BookingRecord] = field(default_factory=list)
    requests: list[RequestEvent] = field(default_factory=list)
    observations: list[SeatObservation] = field(default_factory=list)

    @property
    def booked(self) -> list[BookingRecord]:
        return [b for b in self.bookings if b.ok]

    @property
    def exit_code(self) -> int:
        return 0 if self.status in _OK_STATUSES else 1


class _Finished(Exception):  # noqa: N818 - control flow, not an error
    """Raised to end the run early with a known status."""

    def __init__(self, status: RunStatus, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


def find_opening(opciones: Sequence[Opcion]) -> dt.datetime:
    """The opening time is ``max_date`` of the "clase" option; never hard-coded."""
    for opcion in opciones:
        if opcion.value == "clase":
            return opcion.max_date
    raise UnexpectedResponseError("opciones", 'no option with value "clase"')


def window_is_open(
    disponibilidad: Disponibilidad,
    fechas_previas: Collection[dt.date] | None,
    now: dt.datetime,
    opening: dt.datetime,
) -> bool:
    """Decide from one successful response whether scheduling has opened."""
    if fechas_previas is None:
        # The lock answered with an error before; a 200 means it lifted.
        return now >= opening - CLOCK_TOLERANCE
    if disponibilidad.fechas - set(fechas_previas):
        return True  # a day that was not visible before: unambiguous
    return now >= opening + STALE_LOCK_GRACE


class WindowRunner:
    """Runs one opening window from preparation to verification."""

    def __init__(self, client: AswClient, settings: Settings, clock: Clock) -> None:
        self._client = client
        self._settings = settings
        self._clock = clock
        self._prefs = Preferences.from_settings(settings)
        self._result = RunResult()
        client.add_listener(self._on_request)

    async def run(self) -> RunResult:
        result = self._result
        try:
            await self._run()
        except _Finished as done:
            result.status, result.reason = done.status, done.reason
        except AswError as exc:
            # Never lose bookings that already went through because a later step failed.
            result.reason = f"{type(exc).__name__}: {exc}"
            result.status = self._final_status() if result.booked else RunStatus.FAILED
        log.info(
            "run_finished",
            status=result.status.value,
            reason=result.reason,
            booked=[f"{b.fecha} {b.hora:%H:%M}" for b in result.booked],
        )
        return result

    # --- phases of the run -------------------------------------------------

    async def _run(self) -> None:
        result = self._result
        started = self._clock.now()

        opening = find_opening(await self._client.opciones())
        result.opening = opening
        fechas_previas = await self._snapshot()
        reservas = await self._client.clases()
        log.info(
            "prepared",
            opening=opening.isoformat(),
            snapshot=sorted(d.isoformat() for d in fechas_previas) if fechas_previas else None,
            existing_bookings=len(reservas),
            dry_run=self._settings.dry_run,
        )

        deadline = max(opening, started) + dt.timedelta(
            seconds=self._settings.window_timeout_seconds
        )
        disponibilidad = await self._wait_for_opening(opening, fechas_previas, deadline)
        if disponibilidad is None:
            raise _Finished(RunStatus.NOT_OPENED, "the window did not open before the timeout")

        await self._book_all(disponibilidad, reservas, fechas_previas, deadline)
        await self._verify()
        result.status = self._final_status()

    async def _snapshot(self) -> frozenset[dt.date] | None:
        """Days visible before the opening, or None if the lock hides them."""
        try:
            disponibilidad = await self._client.ver_disponibilidad()
        except BusinessError as exc:
            self._stop_if_no_topics(exc)
            return None
        except _TRANSIENT:
            return None
        self._observe(disponibilidad, None)
        return disponibilidad.fechas

    async def _wait_for_opening(
        self,
        opening: dt.datetime,
        fechas_previas: frozenset[dt.date] | None,
        deadline: dt.datetime,
    ) -> Disponibilidad | None:
        """Poll at a fixed pace from just before the opening until it is detected."""
        interval = dt.timedelta(milliseconds=self._settings.poll_interval_ms)
        offset = dt.timedelta(milliseconds=self._settings.poll_start_offset_ms)
        await self._clock.sleep_until(opening + offset)

        while (tick := self._clock.now()) < deadline:
            try:
                disponibilidad = await self._client.ver_disponibilidad(retry=False)
            except BusinessError as exc:
                self._stop_if_no_topics(exc)  # any other message: still locked
            except _TRANSIENT as exc:
                log.warning("poll_failed", error=type(exc).__name__)
            else:
                self._observe(disponibilidad, fechas_previas)
                now = self._clock.now()
                if window_is_open(disponibilidad, fechas_previas, now, opening):
                    self._result.detected_at = now
                    log.info(
                        "opening_detected",
                        after_opening_ms=round((now - opening).total_seconds() * 1000),
                        days=sorted(d.isoformat() for d in disponibilidad.fechas),
                    )
                    return disponibilidad
            # Start-to-start pacing: at most one poll per interval.
            await self._clock.sleep_until(tick + interval)
        return None

    async def _book_all(
        self,
        disponibilidad: Disponibilidad,
        reservas: list[Reserva],
        fechas_previas: frozenset[dt.date] | None,
        deadline: dt.datetime,
    ) -> None:
        """Ask the strategy what to book next, try it, and repeat with fresh data."""
        result = self._result
        failed: set[tuple[AttemptKind, dt.date, dt.time]] = set()
        first_plan = True

        while self._clock.now() < deadline:
            plan = build_plan(
                disponibilidad, self._prefs, reservas, self._clock.now(), fechas_previas
            )
            if first_plan:
                first_plan = False
                bookable = sum(1 for e in plan.evaluations if e.verdict != Verdict.ALREADY_BOOKED)
                result.target_date = plan.target_date
                result.wanted = min(plan.remaining, bookable)
                result.reason = plan.outcome.value
                log.info(
                    "plan",
                    outcome=plan.outcome.value,
                    target=plan.target_date.isoformat() if plan.target_date else None,
                    wanted=result.wanted,
                    candidates=[f"{i.hora:%H:%M}" for i in plan.intentos],
                )

            candidates = [i for i in plan.intentos if (i.kind, i.fecha, i.hora) not in failed]
            if not candidates:
                return
            intento = candidates[0]
            record = await self._attempt(intento, reservas)
            result.bookings.append(record)
            log.info(
                "booking_attempt",
                hora=f"{record.hora:%H:%M}",
                fecha=record.fecha.isoformat(),
                ok=record.ok,
                detail=record.detail,
                dry_run=record.dry_run,
            )

            if not record.ok:
                failed.add((intento.kind, intento.fecha, intento.hora))
                if record.stop:
                    return
                continue  # next preference, from the same snapshot

            reservas = [*reservas, _as_reserva(record)]
            if len(result.booked) >= result.wanted:
                return
            if self._settings.dry_run:
                continue  # nothing was sent, so there is nothing new to read
            # The topic advanced: refresh before planning the next class.
            try:
                disponibilidad = await self._client.ver_disponibilidad()
            except BusinessError as exc:
                if exc.kind is BusinessErrorKind.NO_PENDING_TOPICS:
                    return
            except _TRANSIENT:
                pass  # keep planning from the last good snapshot
            else:
                self._observe(disponibilidad, fechas_previas)

    async def _attempt(self, intento: Intento, reservas: Sequence[Reserva]) -> BookingRecord:
        """Carry out one attempt and report what happened, without raising on refusals."""
        fecha = intento.fecha

        def record(ok: bool, detail: str | None = None, **extra: bool) -> BookingRecord:
            return BookingRecord(
                intento.kind, fecha, intento.hora, ok, self._clock.now(), detail, **extra
            )

        if self._settings.dry_run:
            return record(True, "dry run: agendar was not sent", dry_run=True)
        try:
            if intento.kind is AttemptKind.SECOND_OPTION:
                mas = await self._client.ver_mas_opciones(
                    intento.tema_id, intento.fecha, intento.hora
                )
                offered = accept_second_option(mas, intento, self._prefs, reservas)
                if offered is None:
                    return record(False, "second option does not fit the preferences")
                fecha = offered
                await self._client.agendar_segunda_opcion(intento.tema_id, fecha, intento.hora)
            else:
                await self._client.agendar(fecha, intento.hora, intento.tipo_clase)
        except BusinessError as exc:
            return record(False, exc.message, stop=exc.kind is BusinessErrorKind.NO_PENDING_TOPICS)
        except _TRANSIENT as exc:
            # The request may have been processed. Ask the server instead of repeating it.
            confirmed = await self._is_booked(fecha, intento.hora)
            detail = f"{type(exc).__name__}; clases shows it booked: {confirmed}"
            return record(confirmed, detail, verified=confirmed)
        return record(True)

    async def _is_booked(self, fecha: dt.date, hora: dt.time) -> bool:
        try:
            clases = await self._client.clases()
        except AswError:
            return False
        return any(c.booked_date == fecha and c.booked_time == hora for c in clases)

    async def _verify(self) -> None:
        """Confirm with ``clases`` that every booking is really there."""
        real = [b for b in self._result.booked if not b.dry_run]
        if not real:
            return
        try:
            clases = await self._client.clases()
        except AswError as exc:
            log.warning("verification_failed", error=type(exc).__name__)
            return
        booked = {(c.booked_date, c.booked_time) for c in clases}
        for record in real:
            record.verified = (record.fecha, record.hora) in booked
        log.info("verified", verified=sum(1 for b in real if b.verified), expected=len(real))

    # --- helpers -----------------------------------------------------------

    def _final_status(self) -> RunStatus:
        result = self._result
        if self._settings.dry_run:
            return RunStatus.DRY_RUN
        if result.wanted == 0:
            return RunStatus.NOTHING_TO_DO
        booked = len(result.booked)
        if booked == 0:
            return RunStatus.NOTHING_BOOKED
        return RunStatus.BOOKED_ALL if booked >= result.wanted else RunStatus.BOOKED_PARTIAL

    def _stop_if_no_topics(self, exc: BusinessError) -> None:
        if exc.kind is BusinessErrorKind.NO_PENDING_TOPICS:
            raise _Finished(RunStatus.NOTHING_TO_DO, Outcome.NO_PENDING_TOPICS.value) from None

    def _observe(
        self, disponibilidad: Disponibilidad, fechas_previas: frozenset[dt.date] | None
    ) -> None:
        if not disponibilidad.horarios:
            return
        target, _ = pick_target_date(disponibilidad.fechas, fechas_previas)
        day = disponibilidad.day(target)
        if day is None:  # pragma: no cover - target comes from the same response
            return
        seats = {
            hora: slot.disponible
            for hora in self._prefs.target_hours
            if (slot := day.slot(hora)) is not None
        }
        self._result.observations.append(SeatObservation(self._clock.now(), target, seats))

    def _on_request(self, record: RequestRecord) -> None:
        ended = self._clock.now()
        started = ended - dt.timedelta(milliseconds=record.rtt_ms)
        self._result.requests.append(
            RequestEvent(record.endpoint, started, record.rtt_ms, record.status, record.error)
        )


def _as_reserva(record: BookingRecord) -> Reserva:
    """Stand-in for a booking we just made, so the next plan counts it."""
    return Reserva(id=0, fecha=record.fecha.isoformat(), hora=record.hora.strftime("%H:%M:%S"))
