"""FastAPI server that imitates the ASW student API, with a simulated opening.

It mirrors what spec section 3 documents: every endpoint is POST, auth is a
Bearer JWT, business refusals are ``500 {"error": ...}`` and auth failures use
the AdonisJS body. Three things are *assumptions* until the phase 4 observer
reports the real behaviour, and each is a ``MockConfig`` field:

* what ``ver_disponibilidad`` answers during the lock (``locked_*``),
* the text of the "no seats" refusal (``no_seats_message``),
* the success body of ``agendar``.

The clock is injected, so a test decides when "now" is and when the window opens.
"""

import asyncio
import base64
import binascii
import datetime as dt
import hashlib
import hmac
import json
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from email.utils import format_datetime
from types import TracebackType
from typing import Any, Self
from zoneinfo import ZoneInfo

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

BOGOTA = ZoneInfo("America/Bogota")
API_PREFIX = "/api/student"
TEST_JWT_KEY = "mock-server-signing-key"

# id -> start, the ten fixed 1.5 h blocks.
BLOCKS: dict[int, str] = {
    1: "06:00:00",
    2: "07:30:00",
    3: "09:00:00",
    4: "10:30:00",
    5: "12:00:00",
    6: "13:30:00",
    7: "15:00:00",
    8: "16:30:00",
    9: "18:00:00",
    10: "19:30:00",
}
BLOCK_IDS = {hora: block_id for block_id, hora in BLOCKS.items()}
# Seats ASW really showed for a new day (spec 3.4).
DEFAULT_SEATS = dict(zip(BLOCKS.values(), (18, 15, 24, 34, 24, 6, 3, 41, 36, 18), strict=True))

DAY_NAMES = ("Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo")
MONTH_NAMES = (
    "Enero",
    "Febrero",
    "Marzo",
    "Abril",
    "Mayo",
    "Junio",
    "Julio",
    "Agosto",
    "Septiembre",
    "Octubre",
    "Noviembre",
    "Diciembre",
)
CANCEL_TOO_LATE = "Esta clase ya no es posible cancelarla."
CANCEL_ALLOWED = "Recuerda que debes cancelar con mínimo 24 horas…"
NO_PENDING_TOPICS = "No tiene más temas pendientes."
MAX_CLASSES_PER_DAY = 5
ONE_HOUR = dt.timedelta(hours=1)
ONE_DAY = dt.timedelta(days=1)

Reply = tuple[int, Any]


# --- JWT (HS256) with the standard library ----------------------------------


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign_token(payload: dict[str, Any], key: str = TEST_JWT_KEY) -> str:
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    signature = hmac.new(key.encode(), f"{header}.{body}".encode(), hashlib.sha256).digest()
    return f"{header}.{body}.{_b64(signature)}"


def decode_token(
    token: str | None, key: str = TEST_JWT_KEY
) -> tuple[dict[str, Any], None] | tuple[None, str]:
    """Return (payload, None) or (None, reason) with the reasons AdonisJS reports."""
    if not token:
        return None, "jwt must be provided"
    parts = token.split(".")
    if len(parts) != 3:
        return None, "invalid token"
    try:
        json.loads(_unb64(parts[0]))
        payload = json.loads(_unb64(parts[1]))
    except (ValueError, binascii.Error):
        return None, "invalid token"
    expected = hmac.new(key.encode(), f"{parts[0]}.{parts[1]}".encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(_b64(expected), parts[2]):
        return None, "invalid signature"
    if not isinstance(payload, dict):
        return None, "invalid token"
    return payload, None


# --- configuration and state -------------------------------------------------


def _real_now() -> dt.datetime:
    return dt.datetime.now(BOGOTA)


@dataclass
class Fault:
    """Make the next request to ``endpoint`` fail like a broken proxy would.

    With ``process=True`` the request is carried out first and only the
    response is lost: the case where a retry would book twice.
    """

    endpoint: str
    status: int = 502
    process: bool = False


@dataclass
class MockConfig:
    open_at: dt.datetime
    clock: Callable[[], dt.datetime] = _real_now
    new_day_seats: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_SEATS))
    today_seats: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_SEATS))
    # Competition: every interval, other students take this many seats per block.
    drain_interval_ms: int = 200
    drain_per_tick: dict[str, int] = field(default_factory=dict)
    # Blocks whose seats vanish right after the bot first sees them.
    stolen_after_view: frozenset[str] = frozenset()
    # Seats on the day offered by "Más opciones" (same for every block).
    second_option_seats: int = 18
    # Lock behaviour: "error" answers locked_status/locked_message, "stale"
    # answers 200 with the days that were already visible.
    locked_mode: str = "error"
    locked_status: int = 500
    locked_message: str = "Programación bloqueada"
    no_seats_message: str = "No hay cupos disponibles para este horario."
    latency_ms: float = 0.0
    pending_topics: int = 10
    # None accepts any credentials.
    email: str | None = None
    password: str | None = None
    jwt_key: str = TEST_JWT_KEY


@dataclass(frozen=True)
class LoggedRequest:
    at: dt.datetime
    method: str
    endpoint: str
    status: int
    body: dict[str, Any]


def _label(hora: str, *, short: bool = False) -> str:
    """'06:00:00' -> '6:00 am - 7:30 am' (or '6:00am')."""

    def fmt(moment: dt.datetime, sep: str) -> str:
        suffix = "am" if moment.hour < 12 else "pm"
        return f"{moment.hour % 12 or 12}:{moment.minute:02d}{sep}{suffix}"

    start = dt.datetime.combine(dt.date(2000, 1, 1), dt.time.fromisoformat(hora), tzinfo=BOGOTA)
    if short:
        return fmt(start, "")
    return f"{fmt(start, ' ')} - {fmt(start + dt.timedelta(minutes=90), ' ')}"


def next_working_day(day: dt.date) -> dt.date:
    following = day + ONE_DAY
    while following.isoweekday() > 5:
        following += ONE_DAY
    return following


class MockAsw:
    """The fake school: seats, topics, bookings and the request log."""

    def __init__(self, config: MockConfig) -> None:
        self.config = config
        self.requests: list[LoggedRequest] = []
        self.bookings: list[dict[str, Any]] = []
        self.faults: list[Fault] = []
        self.topic_index = 0
        self.token_generation = 1
        self._taken: dict[tuple[dt.date, str], int] = {}
        self._stolen: set[tuple[dt.date, str]] = set()
        self._next_id = 5001

    # --- time and visibility ---

    def now(self) -> dt.datetime:
        return self.config.clock().astimezone(BOGOTA)

    @property
    def today(self) -> dt.date:
        return self.config.open_at.astimezone(BOGOTA).date()

    @property
    def new_day(self) -> dt.date:
        return self.today + ONE_DAY

    def is_open(self, now: dt.datetime) -> bool:
        return now >= self.config.open_at

    def visible_days(self, now: dt.datetime) -> list[dt.date]:
        # Newest first: the real API does not sort its days either.
        return [self.new_day, self.today] if self.is_open(now) else [self.today]

    def revoke_tokens(self) -> None:
        """Invalidate every token issued so far (server-side revocation)."""
        self.token_generation += 1

    # --- seats ---

    def competitor_seats(self, hora: str, now: dt.datetime) -> int:
        """Seats left on the new day counting only what other students took."""
        initial = self.config.new_day_seats.get(hora, 0)
        elapsed_ms = (now - self.config.open_at).total_seconds() * 1000
        ticks = max(0, int(elapsed_ms // self.config.drain_interval_ms))
        return max(0, initial - ticks * self.config.drain_per_tick.get(hora, 0))

    def seats(self, day: dt.date, hora: str, now: dt.datetime) -> int:
        if (day, hora) in self._stolen:
            return 0
        if day == self.new_day:
            base = self.competitor_seats(hora, now)
        elif day == self.today:
            base = self.config.today_seats.get(hora, 0)
        else:
            base = self.config.second_option_seats
        return max(0, base - self._taken.get((day, hora), 0))

    # --- payload builders ---

    def _topic(self) -> dict[str, Any]:
        number = self.topic_index + 1
        return {
            "id": 2563 + self.topic_index,
            "curso_id": 17,
            "nombre": f"1-2-3  / UNIT {number}",
            "orden": 5 + self.topic_index,
            "tipo_clase": "regular",
            "tipo": "regular_17",
            "programable": 1,
            "activo": 1,
            "unidad": f"UNIT {number}",
            "class_topic": "Warm Up: Getting to know you",
        }

    def _hour(self, day: dt.date, hora: str, now: dt.datetime) -> dict[str, Any]:
        start = dt.datetime.combine(day, dt.time.fromisoformat(hora), tzinfo=BOGOTA)
        return {
            "id": BLOCK_IDS[hora],
            "nombre": _label(hora),
            "hora": hora,
            "nombre_corto": _label(hora, short=True),
            "marca_tiempo": start.replace(tzinfo=None).isoformat(),
            "timestamp": int(start.timestamp() * 1000),
            "politica_cancel": CANCEL_ALLOWED if start - now >= ONE_DAY else CANCEL_TOO_LATE,
            "disponible": self.seats(day, hora, now),
        }

    def _day(
        self, day: dt.date, now: dt.datetime, hours: list[str] | None = None
    ) -> dict[str, Any]:
        if hours is None:
            # Only blocks that can still be scheduled: up to one hour before they start.
            hours = [
                hora
                for hora in BLOCKS.values()
                if dt.datetime.combine(day, dt.time.fromisoformat(hora), tzinfo=BOGOTA) - now
                >= ONE_HOUR
            ]
        stamp = dt.datetime.combine(day, now.timetz())
        return {
            "fecha": day.isoformat(),
            "texto": f"{DAY_NAMES[day.weekday()]} {day.day} de {MONTH_NAMES[day.month - 1]}",
            "dia_semana": day.isoweekday() % 7,
            "timestamp": int(stamp.timestamp() * 1000),
            "date": stamp.astimezone(dt.UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "horas": [self._hour(day, hora, now) for hora in hours],
        }

    def _locked(self) -> Reply:
        return self.config.locked_status, {"error": self.config.locked_message}

    # --- endpoints ---

    def login(self, body: dict[str, Any], now: dt.datetime) -> Reply:
        user = body.get("user") if isinstance(body.get("user"), dict) else {}
        config = self.config
        wrong_email = config.email is not None and user.get("email") != config.email
        wrong_password = config.password is not None and user.get("password") != config.password
        if wrong_email or wrong_password:
            # The real refusal is not documented yet; 401 is our assumption.
            return 401, {"message": "E_PASSWORD_MISMATCH: Cannot verify user password"}
        payload = {"uid": 1, "data": {"gen": self.token_generation}, "iat": int(now.timestamp())}
        return 200, {"token": sign_token(payload, config.jwt_key)}

    def opciones(self, _body: dict[str, Any], _now: dt.datetime) -> Reply:
        opening = self.config.open_at.astimezone(BOGOTA).replace(tzinfo=None)
        window = {
            "min_date": opening.replace(hour=0, minute=0, second=0).isoformat(),
            "max_date": opening.isoformat(),
        }
        return 200, [
            {"text": "Clase", "value": "clase", "predeterminada": True, **window},
            {"text": "Tutoría", "value": "tutoria", "predeterminada": False, **window},
        ]

    def ver_disponibilidad(self, _body: dict[str, Any], now: dt.datetime) -> Reply:
        if not self.is_open(now) and self.config.locked_mode == "error":
            return self._locked()
        if self.topic_index >= self.config.pending_topics:
            return 500, {"error": NO_PENDING_TOPICS}
        days = self.visible_days(now)
        reply = {"tema": self._topic(), "horarios": [self._day(day, now) for day in days]}
        if self.is_open(now):
            for hora in self.config.stolen_after_view:
                self._stolen.add((self.new_day, hora))
        return 200, reply

    def ver_mas_opciones(self, body: dict[str, Any], now: dt.datetime) -> Reply:
        if not self.is_open(now):
            return self._locked()
        hora = str(body.get("hora"))
        if hora not in BLOCK_IDS:
            return 500, {"error": "Horario no válido."}
        offered = next_working_day(dt.date.fromisoformat(str(body.get("fecha"))))
        return 200, {"tema": self._topic(), "horarios": self._day(offered, now, [hora])}

    def agendar(self, body: dict[str, Any], now: dt.datetime) -> Reply:
        if not self.is_open(now):
            return self._locked()
        if self.topic_index >= self.config.pending_topics:
            return 500, {"error": NO_PENDING_TOPICS}
        hora = str(body.get("hora"))
        try:
            day = dt.date.fromisoformat(str(body.get("dia")))
        except ValueError:
            return 500, {"error": "Fecha no válida."}
        allowed = set(self.visible_days(now))
        if body.get("segunda_opcion"):
            allowed |= {next_working_day(d) for d in self.visible_days(now)}
        if hora not in BLOCK_IDS or day not in allowed:
            return 500, {"error": "Horario no disponible."}
        start = dt.datetime.combine(day, dt.time.fromisoformat(hora), tzinfo=BOGOTA)
        if start - now < ONE_HOUR:
            return 500, {"error": "Solo es posible programar hasta 1 hora antes de la clase."}
        same_day = [b for b in self.bookings if b["fecha"] == day.isoformat()]
        if len(same_day) >= MAX_CLASSES_PER_DAY:
            return 500, {"error": "Ya tiene el máximo de clases programadas para este día."}
        if any(b["hora"] == hora for b in same_day):
            return 500, {"error": "Ya tiene una clase programada en este horario."}
        if self.seats(day, hora, now) <= 0:
            return 500, {"error": self.config.no_seats_message}

        topic = self._topic()
        self._taken[(day, hora)] = self._taken.get((day, hora), 0) + 1
        self.bookings.append(
            {
                "id": self._next_id,
                "fecha": day.isoformat(),
                "hora": hora,
                "fecha_bloque": start.replace(tzinfo=None).isoformat(),
                "estado": "programada",
                "tema": topic["nombre"],
                "tema_id": topic["id"],
                "tipo_clase": topic["tipo_clase"],
                "tipo_solicitud": body.get("tipo_solicitud"),
                "sede": "SUBA",
                "sede_id": 4,
                "politica_cancel_tiempo": 24,
                "politica_cancel_mensaje": CANCEL_ALLOWED,
                "link_activo": 0,
                "asistencia_fecha": None,
                "observaciones": None,
            }
        )
        self._next_id += 1
        self.topic_index += 1
        texto = f"{DAY_NAMES[day.weekday()]} {day.day} de {MONTH_NAMES[day.month - 1]}"
        return 200, {
            "fecha": day.isoformat(),
            "hora": hora,
            "mensaje": f"Programación exitosa para {texto} {_label(hora, short=True)}",
        }

    def clases(self, _body: dict[str, Any], _now: dt.datetime) -> Reply:
        return 200, [dict(booking) for booking in self.bookings]

    def cancelar(self, body: dict[str, Any], now: dt.datetime) -> Reply:
        booking = next((b for b in self.bookings if b["id"] == body.get("agendamiento_id")), None)
        if booking is None:
            return 500, {"error": "La clase no existe."}
        start = dt.datetime.fromisoformat(booking["fecha_bloque"]).replace(tzinfo=BOGOTA)
        if start - now < ONE_DAY:
            return 500, {"error": CANCEL_TOO_LATE}
        self.bookings.remove(booking)
        key = (dt.date.fromisoformat(booking["fecha"]), booking["hora"])
        self._taken[key] = self._taken.get(key, 1) - 1
        return 200, {"mensaje": "Clase cancelada."}

    def curso_actual(self, _body: dict[str, Any], _now: dt.datetime) -> Reply:
        return 200, {
            "id": 17,
            "codigo": "C_EN-A2",
            "nombre": "Connectivity 1",
            "capacidad": 6,
            "nivel": {"codigo": "A2"},
        }

    def sede(self, _body: dict[str, Any], _now: dt.datetime) -> Reply:
        return 200, {"id": 4, "nombre": "SUBA", "ciudad": "BOGOTA", "es_virtual": 0}

    # --- dispatch ---

    def handle(self, method: str, endpoint: str, token: str | None, body: dict[str, Any]) -> Reply:
        """Route one request the way the real server does and return (status, body)."""
        handlers: dict[str, Callable[[dict[str, Any], dt.datetime], Reply]] = {
            "login": self.login,
            "opciones": self.opciones,
            "ver_disponibilidad": self.ver_disponibilidad,
            "ver_mas_opciones": self.ver_mas_opciones,
            "agendar": self.agendar,
            "clases": self.clases,
            "cancelar": self.cancelar,
            "curso_actual": self.curso_actual,
            "sede": self.sede,
        }
        handler = handlers.get(endpoint)
        if method != "POST" or handler is None:
            return 404, {
                "message": f"E_ROUTE_NOT_FOUND: Route not found {method} {API_PREFIX}/{endpoint}",
                "name": "HttpException",
                "code": "E_ROUTE_NOT_FOUND",
                "status": 404,
            }
        if endpoint != "login":
            payload, reason = decode_token(token, self.config.jwt_key)
            if payload is not None and payload.get("data", {}).get("gen") != self.token_generation:
                reason = "invalid token"
            if reason is not None:
                return 401, {
                    "message": f"E_INVALID_JWT_TOKEN: {reason}",
                    "name": "InvalidJwtToken",
                    "code": "E_INVALID_JWT_TOKEN",
                    "status": 401,
                }
        return handler(body, self.now())

    def take_fault(self, endpoint: str) -> Fault | None:
        for fault in self.faults:
            if fault.endpoint == endpoint:
                self.faults.remove(fault)
                return fault
        return None


# --- HTTP layer --------------------------------------------------------------


def create_app(config: MockConfig) -> FastAPI:
    state = MockAsw(config)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.mock = state

    @app.api_route(
        API_PREFIX + "/{endpoint:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"]
    )
    async def dispatch(endpoint: str, request: Request) -> Response:
        half_trip = config.latency_ms / 2000
        if half_trip:
            await asyncio.sleep(half_trip)  # request travelling to the server

        raw = await request.body()
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        authorization = request.headers.get("authorization", "")
        token = authorization[7:] if authorization.lower().startswith("bearer ") else None

        arrived = state.now()
        fault = state.take_fault(endpoint) if request.method == "POST" else None
        if fault is not None and not fault.process:
            status, payload = fault.status, None
        else:
            status, payload = state.handle(request.method, endpoint, token, body)
            if fault is not None:
                status, payload = fault.status, None
        # Never keep credentials in the log, not even fake ones.
        logged_body = {} if endpoint == "login" else body
        state.requests.append(LoggedRequest(arrived, request.method, endpoint, status, logged_body))

        if half_trip:
            await asyncio.sleep(half_trip)  # response travelling back
        headers = {
            "Date": format_datetime(state.now().astimezone(dt.UTC), usegmt=True),
            "Server": "nginx",
        }
        if payload is None:
            return PlainTextResponse("Bad Gateway", status_code=status, headers=headers)
        return JSONResponse(payload, status_code=status, headers=headers)

    return app


def ceil_to_second(moment: dt.datetime) -> dt.datetime:
    """``opciones`` publishes the opening with one-second resolution."""
    if moment.microsecond == 0:
        return moment
    return moment.replace(microsecond=0) + dt.timedelta(seconds=1)


def create_app_from_env() -> FastAPI:
    """Factory for running by hand: ``uvicorn mock_server.app:create_app_from_env --factory``."""
    delay = float(os.environ.get("MOCK_OPEN_AFTER_SECONDS", "10"))
    config = MockConfig(
        open_at=ceil_to_second(_real_now() + dt.timedelta(seconds=delay)),
        locked_mode=os.environ.get("MOCK_LOCKED_MODE", "error"),
        locked_status=int(os.environ.get("MOCK_LOCKED_STATUS", "500")),
        locked_message=os.environ.get("MOCK_LOCKED_MESSAGE", "Programación bloqueada"),
        latency_ms=float(os.environ.get("MOCK_LATENCY_MS", "0")),
        drain_per_tick={"07:30:00": 1, "09:00:00": 1, "10:30:00": 1},
    )
    return create_app(config)


class MockServer:
    """Run the mock with uvicorn on a free local port, in a background thread.

    A separate thread keeps the server responsive even if the code under test
    blocks its own event loop (as the phase 5 busy-wait does on purpose).
    """

    def __init__(self, config: MockConfig) -> None:
        self.app = create_app(config)
        self.state: MockAsw = self.app.state.mock
        self.base_url = ""
        self._server = uvicorn.Server(
            uvicorn.Config(
                self.app,
                host="127.0.0.1",
                port=0,
                log_level="warning",
                date_header=False,
                server_header=False,
                lifespan="off",
            )
        )
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def __enter__(self) -> Self:
        self._thread.start()
        deadline = time.monotonic() + 10
        while not self._server.started:
            if time.monotonic() > deadline or not self._thread.is_alive():
                raise RuntimeError("mock server did not start")
            time.sleep(0.01)
        port = self._server.servers[0].sockets[0].getsockname()[1]
        self.base_url = f"http://127.0.0.1:{port}{API_PREFIX}/"
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10)
