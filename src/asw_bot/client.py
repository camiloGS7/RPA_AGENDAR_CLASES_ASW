"""Async HTTP client for the ASW API: the only module that talks to the network.

Responsibilities, in the order a request goes through them:

1. Dry-run guard: write endpoints are refused before anything is sent.
2. Authentication: reuse the stored token; log in only when the API says 401.
3. Transport: one keep-alive connection, explicit timeouts, timing of each call.
4. Classification: turn status + body into a typed result or a typed error.
5. Retries: only where repeating the request cannot cause harm.

Nothing here logs request bodies, response bodies or the Authorization header.
"""

import asyncio
import datetime as dt
import time
from collections.abc import Callable
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Self

import httpx
import structlog
from pydantic import BaseModel, ValidationError
from structlog.typing import FilteringBoundLogger
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_exponential

from asw_bot.config import Settings
from asw_bot.errors import (
    AuthError,
    BusinessError,
    DryRunError,
    NetworkError,
    RouteError,
    ServerError,
    UnexpectedResponseError,
)
from asw_bot.models import AgendarResult, Disponibilidad, MasOpciones, Opcion, Reserva
from asw_bot.token_store import TokenStore

log: FilteringBoundLogger = structlog.get_logger(__name__)

# The web app takes the zone from the OS; on Railway that would be "UTC".
BOOKING_TIMEZONE = "America/Bogota"
WRITE_ENDPOINTS = frozenset({"agendar", "cancelar"})

CONNECT_TIMEOUT_S = 3.0
READ_TIMEOUT_S = 5.0

# Transport failures where we know the request never reached the server.
_NOT_DELIVERED = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


@dataclass(frozen=True, slots=True)
class ApiResponse:
    """What came back from one HTTP call, before any interpretation."""

    status: int
    data: Any
    is_json: bool
    server_date: str | None
    rtt_ms: float


@dataclass(frozen=True, slots=True)
class RequestRecord:
    """Timing and outcome of one HTTP call, for listeners. Never carries bodies."""

    endpoint: str
    attempt: int
    rtt_ms: float
    status: int | None
    error: str | None
    server_date: str | None


RequestListener = Callable[[RequestRecord], None]


def _retry_read(exc: BaseException) -> bool:
    """Reads are idempotent: any server or network failure may be retried."""
    return isinstance(exc, ServerError | NetworkError)


def _retry_write(exc: BaseException) -> bool:
    """Writes are retried only when the request provably never left.

    After a timeout or a 502 the booking may have gone through; repeating it
    could book a second class. The caller must check ``clases`` instead.
    """
    return isinstance(exc, NetworkError) and not exc.maybe_delivered


def _describe(exc: ValidationError) -> str:
    """Summarise a schema mismatch without echoing the received values."""
    errors = exc.errors(include_input=False, include_url=False)
    return "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['type']}" for e in errors[:5])


def _parse[T: BaseModel](model: type[T], data: Any, endpoint: str) -> T:
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        # "from None": the original error embeds the raw input in its message.
        raise UnexpectedResponseError(endpoint, _describe(exc)) from None


def _parse_list[T: BaseModel](model: type[T], data: Any, endpoint: str) -> list[T]:
    if not isinstance(data, list):
        raise UnexpectedResponseError(endpoint, "expected a JSON array")
    return [_parse(model, item, endpoint) for item in data]


def _parse_object(data: Any, endpoint: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise UnexpectedResponseError(endpoint, "expected a JSON object")
    return data


def _booking_result(data: Any) -> AgendarResult:
    """Wrap whatever ``agendar`` returned on success.

    Success is defined as "no error key", and the real body is not captured
    yet. If it turns out not to be an object, the booking still happened, so
    this must not raise: the value is kept under ``mensaje``.
    """
    if isinstance(data, dict):
        return _parse(AgendarResult, data, "agendar")
    return AgendarResult(mensaje=data)


def _fmt_hora(hora: dt.time) -> str:
    return hora.strftime("%H:%M:%S")


class AswClient:
    """Typed access to the ASW student API.

    Use as an async context manager so the connection is closed on exit::

        async with AswClient(settings, store) as client:
            disponibilidad = await client.ver_disponibilidad()
    """

    def __init__(
        self,
        settings: Settings,
        token_store: TokenStore,
        *,
        max_retries: int = 2,
        retry_wait_s: float = 0.4,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._store = token_store
        self._max_retries = max_retries
        self._retry_wait_s = retry_wait_s
        self._token: str | None = None
        self._login_lock = asyncio.Lock()
        self._listeners: list[RequestListener] = []
        self._http = httpx.AsyncClient(
            base_url=settings.asw_base_url,
            timeout=httpx.Timeout(READ_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            http2=settings.http2,
            # Tests plug the mock server in here to run without sockets.
            transport=transport,
        )

    def __repr__(self) -> str:
        return (
            f"AswClient(base_url={self._settings.asw_base_url!r}, dry_run={self._settings.dry_run})"
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    def add_listener(self, listener: RequestListener) -> None:
        """Be told about every HTTP round trip (timeline, event log, clock sync)."""
        self._listeners.append(listener)

    def _notify(self, record: RequestRecord) -> None:
        for listener in self._listeners:
            listener(record)

    # --- read endpoints ---------------------------------------------------

    async def opciones(self) -> list[Opcion]:
        """Lock window. The opening time is ``max_date`` of the "clase" option."""
        return _parse_list(Opcion, await self._post("opciones", {}), "opciones")

    async def ver_disponibilidad(self, *, retry: bool = True) -> Disponibilidad:
        """Next topic and free seats for the visible days. Sent without a body.

        Pass ``retry=False`` when polling: the next poll is the retry, and an
        automatic one would break the pacing of one request per interval.
        """
        endpoint = "ver_disponibilidad"
        return _parse(Disponibilidad, await self._post(endpoint, retry=retry), endpoint)

    async def ver_mas_opciones(self, tema_id: int, fecha: dt.date, hora: dt.time) -> MasOpciones:
        """Same hour on the next working day (the app's "Más opciones")."""
        endpoint = "ver_mas_opciones"
        body = {"tema_id": tema_id, "fecha": fecha.isoformat(), "hora": _fmt_hora(hora)}
        return _parse(MasOpciones, await self._post(endpoint, body), endpoint)

    async def clases(self) -> list[Reserva]:
        """My current bookings; used to verify what ``agendar`` did."""
        return _parse_list(Reserva, await self._post("clases", {}), "clases")

    async def curso_actual(self) -> dict[str, Any]:
        return _parse_object(await self._post("curso_actual", {}), "curso_actual")

    async def sede(self) -> dict[str, Any]:
        return _parse_object(await self._post("sede", {}), "sede")

    # --- write endpoints (blocked while DRY_RUN is on) ---------------------

    async def agendar(self, dia: dt.date, hora: dt.time, tipo_clase: str | None) -> AgendarResult:
        """Book the next topic at ``dia`` + ``hora``. Mirrors the "Programar" button."""
        body = {
            "zona_horaria": BOOKING_TIMEZONE,
            "tema_id": None,
            "hora": _fmt_hora(hora),
            "dia": dia.isoformat(),
            "tipo_clase": tipo_clase,
            # The app has a bug that always overwrites this value; we mirror it.
            "tipo_solicitud": "normal",
        }
        return _booking_result(await self._post("agendar", body))

    async def agendar_segunda_opcion(
        self, tema_id: int, dia: dt.date, hora: dt.time
    ) -> AgendarResult:
        """Book the slot offered by ``ver_mas_opciones``. Body as the app sends it."""
        body = {
            "tema_id": tema_id,
            "hora": _fmt_hora(hora),
            "dia": dia.isoformat(),
            "tipo_clase": None,
            "tipo_solicitud": "normal",
            "segunda_opcion": True,
        }
        return _booking_result(await self._post("agendar", body))

    async def cancelar(self, agendamiento_id: int) -> Any:
        """Cancel a booking by the ``id`` that ``clases`` reports."""
        return await self._post("cancelar", {"agendamiento_id": agendamiento_id})

    # --- authentication ---------------------------------------------------

    async def login(self) -> None:
        """Force a fresh login, replacing whatever token we had."""
        await self._login(stale=self._token)

    async def _ensure_token(self) -> str:
        if self._token is None:
            self._token = self._store.load()
        if self._token is None:
            return await self._login(stale=None)
        return self._token

    async def _login(self, stale: str | None) -> str:
        async with self._login_lock:
            # Another task may have logged in while we waited for the lock.
            if self._token is not None and self._token != stale:
                return self._token

            password = self._settings.asw_password.get_secret_value()
            if not self._settings.asw_email or not password:
                raise AuthError("cannot log in: ASW_EMAIL or ASW_PASSWORD is not set")

            self._token = None
            self._store.clear()
            body = {
                "user": {
                    "email": self._settings.asw_email,
                    "password": password,
                    "mantener_sesion": True,
                }
            }
            response = await self._send("login", body, token=None, attempt=1)
            try:
                data = self._classify("login", response)
            except BusinessError as exc:
                raise AuthError(f"login rejected: {exc.message}") from None

            token = data.get("token") if isinstance(data, dict) else None
            if not isinstance(token, str) or not token:
                raise AuthError("login response has no token")

            # Field names only, never values: the response is not documented yet.
            log.info("login_ok", status=response.status, fields=sorted(data))
            self._token = token
            self._store.save(token)
            return token

    # --- transport --------------------------------------------------------

    async def _post(
        self, endpoint: str, body: dict[str, Any] | None = None, *, retry: bool = True
    ) -> Any:
        """POST with auth, one relogin on 401, and retries where they are safe."""
        write = endpoint in WRITE_ENDPOINTS
        if write and self._settings.dry_run:
            log.warning("write_blocked_by_dry_run", endpoint=endpoint)
            raise DryRunError(endpoint)

        relogged = False
        retrying = AsyncRetrying(
            stop=stop_after_attempt(self._max_retries + 1 if retry else 1),
            wait=wait_exponential(multiplier=self._retry_wait_s),
            retry=retry_if_exception(_retry_write if write else _retry_read),
            reraise=True,
        )
        async for attempt in retrying:
            with attempt:
                number = attempt.retry_state.attempt_number
                token = await self._ensure_token()
                response = await self._send(endpoint, body, token=token, attempt=number)
                if response.status == httpx.codes.UNAUTHORIZED and not relogged:
                    relogged = True
                    log.info("token_rejected", endpoint=endpoint)
                    token = await self._login(stale=token)
                    response = await self._send(endpoint, body, token=token, attempt=number)
                return self._classify(endpoint, response)
        raise AssertionError("unreachable")  # pragma: no cover

    async def _send(
        self, endpoint: str, body: dict[str, Any] | None, *, token: str | None, attempt: int
    ) -> ApiResponse:
        """One HTTP round trip. Logs timing and status, never headers or bodies."""
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        started = time.perf_counter()
        try:
            response = await self._http.post(endpoint, json=body, headers=headers)
        except httpx.TransportError as exc:
            cause = type(exc).__name__
            failed_ms = round((time.perf_counter() - started) * 1000, 1)
            log.warning(
                "http_error", endpoint=endpoint, attempt=attempt, error=cause, duration_ms=failed_ms
            )
            self._notify(RequestRecord(endpoint, attempt, failed_ms, None, cause, None))
            raise NetworkError(
                endpoint, cause, maybe_delivered=not isinstance(exc, _NOT_DELIVERED)
            ) from None

        rtt_ms = round((time.perf_counter() - started) * 1000, 1)
        try:
            data, is_json = response.json(), True
        except ValueError:
            data, is_json = None, False

        log.info(
            "http_request",
            endpoint=endpoint,
            status=response.status_code,
            duration_ms=rtt_ms,
            attempt=attempt,
            server_date=response.headers.get("date"),
        )
        self._notify(
            RequestRecord(
                endpoint, attempt, rtt_ms, response.status_code, None, response.headers.get("date")
            )
        )
        return ApiResponse(
            status=response.status_code,
            data=data,
            is_json=is_json,
            server_date=response.headers.get("date"),
            rtt_ms=rtt_ms,
        )

    @staticmethod
    def _classify(endpoint: str, response: ApiResponse) -> Any:
        """Apply the error table of spec 3.5. Returns the JSON body on success."""
        status, data = response.status, response.data

        if status == httpx.codes.UNAUTHORIZED:
            message = data.get("message") if isinstance(data, dict) else None
            code = data.get("code") if isinstance(data, dict) else None
            raise AuthError(str(message or "unauthorized"), str(code) if code else None)
        if status == httpx.codes.NOT_FOUND:
            raise RouteError(endpoint)
        # Business rejections arrive as 500, but "no error key" is the real
        # definition of success, so check the key whatever the status is.
        if isinstance(data, dict) and "error" in data:
            raise BusinessError(str(data["error"]), status)
        if status >= httpx.codes.INTERNAL_SERVER_ERROR:
            raise ServerError(endpoint, status)
        if not httpx.codes.is_success(status):
            raise UnexpectedResponseError(endpoint, f"HTTP {status}")
        if not response.is_json:
            raise UnexpectedResponseError(endpoint, "body is not JSON")
        return data
