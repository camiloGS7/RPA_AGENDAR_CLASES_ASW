"""Simulate one opening window against the local mock server and chart it.

Everything stays on this machine: the mock listens on 127.0.0.1 and the bot is
pointed at it. The mock adds 165 ms of latency per request (what we measured
from Bogota) and other "students" take seats every 200 ms after the opening.

Writes reports/simulacion.html.
"""

import asyncio
import datetime as dt
import logging
from pathlib import Path

import structlog
from mock_server.app import MockAsw, MockConfig, MockServer
from rich.console import Console
from rich.table import Table

from asw_bot.client import AswClient
from asw_bot.clock import ShiftedClock
from asw_bot.config import Settings
from asw_bot.models import BOGOTA
from asw_bot.report import write_run_report
from asw_bot.runner import RunResult, WindowRunner
from asw_bot.token_store import MemoryTokenStore

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "reports" / "simulacion.html"

# A Tuesday at the real opening time; the new day is Wednesday 11.
OPENING = dt.datetime(2026, 11, 10, 4, 40, tzinfo=BOGOTA)
LEAD_SECONDS = 4.2
LATENCY_MS = 165.0
DRAIN_INTERVAL_MS = 200
# Seats other students take from each block on every tick.
DRAIN = {"07:30:00": 1, "09:00:00": 2, "10:30:00": 3}


def simulate() -> tuple[RunResult, MockAsw, Settings]:
    clocks: list[ShiftedClock] = []
    config = MockConfig(
        open_at=OPENING,
        clock=lambda: clocks[0].now(),
        latency_ms=LATENCY_MS,
        drain_interval_ms=DRAIN_INTERVAL_MS,
        drain_per_tick=DRAIN,
    )

    async def run(base_url: str) -> tuple[RunResult, Settings]:
        settings = Settings(
            _env_file=None,
            asw_email="student@example.com",
            asw_password="simulation-only",  # noqa: S106 - the mock accepts anything
            asw_base_url=base_url,
            dry_run=False,  # safe: base_url is the local mock, checked below
        )
        async with AswClient(settings, MemoryTokenStore()) as client:
            return await WindowRunner(client, settings, clocks[0]).run(), settings

    with MockServer(config) as server:
        if not server.base_url.startswith("http://127.0.0.1:"):
            raise RuntimeError("the simulation must only talk to the local mock")
        clocks.append(ShiftedClock(OPENING - dt.timedelta(seconds=LEAD_SECONDS)))
        result, settings = asyncio.run(run(server.base_url))
        return result, server.state, settings


def competitor_curves(
    state: MockAsw, settings: Settings, until_ms: float
) -> dict[dt.time, list[tuple[float, int]]]:
    """Seats each block had on the server, counting only what the other students took."""
    curves: dict[dt.time, list[tuple[float, int]]] = {}
    for hora in settings.target_hours:
        key = hora.strftime("%H:%M:%S")
        points = []
        tick = 0.0
        while tick <= until_ms:
            moment = OPENING + dt.timedelta(milliseconds=tick)
            points.append((tick, state.competitor_seats(key, moment)))
            tick += DRAIN_INTERVAL_MS
        curves[hora] = points
    return curves


def main() -> None:
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING))
    console = Console()
    console.print(f"Simulando la apertura (espera unos {LEAD_SECONDS + 3:.0f} segundos)...")

    result, state, settings = simulate()

    def after_opening(moment: dt.datetime) -> float:
        return (moment - OPENING).total_seconds() * 1000

    last = max((after_opening(r.started_at) + r.rtt_ms for r in result.requests), default=0.0)
    write_run_report(
        result,
        REPORT,
        title="Simulación de la apertura",
        subtitle=(
            f"Servidor simulado en esta máquina, {LATENCY_MS:.0f} ms de latencia por petición y "
            f"competencia que consume cupos cada {DRAIN_INTERVAL_MS} ms. Sondeo cada "
            f"{settings.poll_interval_ms} ms desde T{settings.poll_start_offset_ms} ms."
        ),
        competitor=competitor_curves(state, settings, last + 250),
    )

    table = Table(title=f"Resultado: {result.status.value}")
    table.add_column("Evento")
    table.add_column("ms desde T", justify="right")
    if result.detected_at is not None:
        table.add_row("Apertura detectada", f"+{after_opening(result.detected_at):.0f}")
    for booking in result.bookings:
        outcome = "reservada" if booking.ok else f"no reservada ({booking.detail})"
        table.add_row(f"{booking.hora:%H:%M} {outcome}", f"+{after_opening(booking.at):.0f}")
    console.print(table)

    polls = [r for r in result.requests if r.endpoint == "ver_disponibilidad"]
    rtts = sorted(r.rtt_ms for r in result.requests)
    console.print(
        f"Peticiones: {len(result.requests)} (sondeos: {len(polls)}). "
        f"RTT mediano: {rtts[len(rtts) // 2]:.0f} ms. "
        f"Límite del criterio (1 intervalo + 1 RTT): "
        f"{settings.poll_interval_ms + LATENCY_MS:.0f} ms."
    )
    console.print(f"Reporte: {REPORT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
