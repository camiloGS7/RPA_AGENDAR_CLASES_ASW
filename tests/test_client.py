"""AswClient against a mocked API (respx): no test here touches the network."""

import asyncio
import datetime as dt
import json
import logging
from typing import Any

import httpx
import pytest
import respx
import structlog

from asw_bot.client import AswClient
from asw_bot.errors import (
    AuthError,
    BusinessError,
    BusinessErrorKind,
    DryRunError,
    NetworkError,
    RouteError,
    ServerError,
    UnexpectedResponseError,
)
from asw_bot.token_store import MemoryTokenStore
from helpers import (
    EMAIL,
    INVALID_TOKEN_401,
    NEW_TOKEN,
    OLD_TOKEN,
    PASSWORD,
    load_fixture,
    make_settings,
)

DIA = dt.date(2026, 11, 10)
HORA = dt.time(6, 0)
HTML_500 = httpx.Response(500, text="<html><body>Internal Server Error</body></html>")


def sent_json(route: respx.Route, index: int = -1) -> Any:
    return json.loads(route.calls[index].request.content)


def bearer(route: respx.Route, index: int = -1) -> str:
    return str(route.calls[index].request.headers["authorization"])


# --- conventions of the API ------------------------------------------------


async def test_requests_are_post_with_bearer_and_json_headers(
    api: respx.MockRouter, client: AswClient
) -> None:
    route = api.post("opciones").respond(json=load_fixture("opciones.json"))

    opciones = await client.opciones()

    request = route.calls.last.request
    assert request.method == "POST"
    assert str(request.url) == "https://asw.test/api/student/opciones"
    assert request.headers["authorization"] == f"Bearer {OLD_TOKEN}"
    assert request.headers["accept"] == "application/json"
    assert request.headers["content-type"] == "application/json"
    assert sent_json(route) == {}
    assert [o.value for o in opciones] == ["clase", "tutoria"]


async def test_ver_disponibilidad_is_sent_without_a_body(
    api: respx.MockRouter, client: AswClient
) -> None:
    route = api.post("ver_disponibilidad").respond(json=load_fixture("ver_disponibilidad.json"))

    disponibilidad = await client.ver_disponibilidad()

    assert route.calls.last.request.content == b""
    assert disponibilidad.tema.id == 2563
    assert disponibilidad.fechas == {dt.date(2026, 11, 9), dt.date(2026, 11, 10)}


async def test_ver_mas_opciones_sends_topic_date_and_hour(
    api: respx.MockRouter, client: AswClient
) -> None:
    route = api.post("ver_mas_opciones").respond(json=load_fixture("ver_mas_opciones.json"))

    mas = await client.ver_mas_opciones(2563, DIA, HORA)

    assert sent_json(route) == {"tema_id": 2563, "fecha": "2026-11-10", "hora": "06:00:00"}
    assert mas.horarios.fecha == dt.date(2026, 11, 11)


async def test_clases_and_profile_endpoints(api: respx.MockRouter, client: AswClient) -> None:
    api.post("clases").respond(json=[{"id": 7, "fecha": "2026-11-10", "hora": "06:00:00"}])
    api.post("curso_actual").respond(json={"id": 17, "codigo": "C_EN-A2", "capacidad": 6})
    api.post("sede").respond(json={"id": 4, "nombre": "SUBA", "es_virtual": 0})

    clases = await client.clases()

    assert [(c.id, c.booked_date) for c in clases] == [(7, DIA)]
    assert (await client.curso_actual())["codigo"] == "C_EN-A2"
    assert (await client.sede())["nombre"] == "SUBA"


async def test_empty_bookings_list(api: respx.MockRouter, client: AswClient) -> None:
    api.post("clases").respond(json=[])
    assert await client.clases() == []


# --- authentication --------------------------------------------------------


async def test_401_triggers_one_relogin_and_the_request_succeeds(
    api: respx.MockRouter, client: AswClient, store: MemoryTokenStore
) -> None:
    route = api.post("ver_disponibilidad").mock(
        side_effect=[
            httpx.Response(401, json=INVALID_TOKEN_401),
            httpx.Response(200, json=load_fixture("ver_disponibilidad.json")),
        ]
    )
    login = api.post("login").respond(json={"token": NEW_TOKEN, "otro_campo": 1})

    disponibilidad = await client.ver_disponibilidad()

    assert disponibilidad.tema.id == 2563
    assert route.call_count == 2
    assert login.call_count == 1
    assert bearer(route, 0) == f"Bearer {OLD_TOKEN}"
    assert bearer(route, 1) == f"Bearer {NEW_TOKEN}"
    assert store.load() == NEW_TOKEN
    assert sent_json(login) == {
        "user": {"email": EMAIL, "password": PASSWORD, "mantener_sesion": True}
    }
    assert "authorization" not in login.calls.last.request.headers


async def test_second_401_after_relogin_gives_up(api: respx.MockRouter, client: AswClient) -> None:
    route = api.post("opciones").respond(401, json=INVALID_TOKEN_401)
    login = api.post("login").respond(json={"token": NEW_TOKEN})

    with pytest.raises(AuthError) as excinfo:
        await client.opciones()

    assert excinfo.value.code == "E_INVALID_JWT_TOKEN"
    assert route.call_count == 2
    assert login.call_count == 1


async def test_without_a_stored_token_it_logs_in_first(api: respx.MockRouter) -> None:
    route = api.post("clases").respond(json=[])
    login = api.post("login").respond(json={"token": NEW_TOKEN})
    store = MemoryTokenStore()

    async with AswClient(make_settings(), store) as client:
        await client.clases()

    assert login.call_count == 1
    assert route.call_count == 1
    assert bearer(route) == f"Bearer {NEW_TOKEN}"
    assert store.load() == NEW_TOKEN


async def test_the_token_is_reused_across_requests(
    api: respx.MockRouter, client: AswClient
) -> None:
    api.post("clases").respond(json=[])
    login = api.post("login").respond(json={"token": NEW_TOKEN})

    await client.clases()
    await client.clases()

    assert login.call_count == 0


async def test_concurrent_requests_share_a_single_login(api: respx.MockRouter) -> None:
    async def slow_login(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.01)  # let the second request reach the login lock
        return httpx.Response(200, json={"token": NEW_TOKEN})

    login = api.post("login").mock(side_effect=slow_login)
    route = api.post("clases").respond(json=[])

    async with AswClient(make_settings(), MemoryTokenStore()) as client:
        await asyncio.gather(client.clases(), client.clases())

    assert login.call_count == 1
    assert route.call_count == 2
    assert {bearer(route, 0), bearer(route, 1)} == {f"Bearer {NEW_TOKEN}"}


async def test_explicit_login_replaces_the_token(
    api: respx.MockRouter, client: AswClient, store: MemoryTokenStore
) -> None:
    api.post("login").respond(json={"token": NEW_TOKEN})
    route = api.post("clases").respond(json=[])

    await client.clases()
    await client.login()
    await client.clases()

    assert bearer(route, 0) == f"Bearer {OLD_TOKEN}"
    assert bearer(route, 1) == f"Bearer {NEW_TOKEN}"
    assert store.load() == NEW_TOKEN


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, json={"message": "wrong credentials"}),
        httpx.Response(500, json={"error": "Usuario o clave incorrectos"}),
        httpx.Response(200, json={"usuario": "sin token"}),
        httpx.Response(200, json={"token": ""}),
        httpx.Response(200, json=["not", "an", "object"]),
    ],
)
async def test_failed_login_is_an_auth_error(
    api: respx.MockRouter, response: httpx.Response
) -> None:
    login = api.post("login").mock(return_value=response)
    target = api.post("clases").respond(json=[])
    store = MemoryTokenStore()

    async with AswClient(make_settings(), store) as client:
        with pytest.raises(AuthError):
            await client.clases()

    assert login.call_count == 1
    assert target.call_count == 0
    assert store.load() is None


async def test_login_without_credentials_fails_before_any_request(api: respx.MockRouter) -> None:
    login = api.post("login").respond(json={"token": NEW_TOKEN})

    async with AswClient(make_settings(asw_password=""), MemoryTokenStore()) as client:
        with pytest.raises(AuthError, match="ASW_PASSWORD"):
            await client.clases()

    assert login.call_count == 0


# --- error table (spec 3.5) ------------------------------------------------


async def test_500_with_json_error_is_a_business_error_and_is_not_retried(
    api: respx.MockRouter, client: AswClient
) -> None:
    route = api.post("ver_disponibilidad").respond(
        500, json={"error": "No tiene más temas pendientes."}
    )

    with pytest.raises(BusinessError) as excinfo:
        await client.ver_disponibilidad()

    assert excinfo.value.message == "No tiene más temas pendientes."
    assert excinfo.value.status == 500
    assert excinfo.value.kind is BusinessErrorKind.NO_PENDING_TOPICS
    assert route.call_count == 1


async def test_unknown_business_message_is_classified_as_unknown(
    api: respx.MockRouter, client: AswClient
) -> None:
    api.post("opciones").respond(500, json={"error": "Algo que no conocemos"})

    with pytest.raises(BusinessError) as excinfo:
        await client.opciones()

    assert excinfo.value.kind is BusinessErrorKind.UNKNOWN


async def test_error_key_means_failure_even_with_status_200(
    api: respx.MockRouter, client: AswClient
) -> None:
    api.post("opciones").respond(200, json={"error": "No permitido"})

    with pytest.raises(BusinessError):
        await client.opciones()


async def test_500_without_json_is_retried_twice_then_fails(
    api: respx.MockRouter, client: AswClient
) -> None:
    route = api.post("opciones").mock(return_value=HTML_500)

    with pytest.raises(ServerError) as excinfo:
        await client.opciones()

    assert excinfo.value.status == 500
    assert route.call_count == 3  # first attempt + 2 retries


async def test_transient_502_recovers_on_retry(api: respx.MockRouter, client: AswClient) -> None:
    route = api.post("opciones").mock(
        side_effect=[
            httpx.Response(502, text="Bad Gateway"),
            httpx.Response(200, json=load_fixture("opciones.json")),
        ]
    )

    opciones = await client.opciones()

    assert len(opciones) == 2
    assert route.call_count == 2


async def test_500_with_json_but_no_error_key_is_a_server_error(
    api: respx.MockRouter, client: AswClient
) -> None:
    api.post("opciones").respond(500, json={"message": "E_RUNTIME", "name": "Exception"})

    with pytest.raises(ServerError):
        await client.opciones()


async def test_404_is_a_route_error_and_is_not_retried(
    api: respx.MockRouter, client: AswClient
) -> None:
    route = api.post("opciones").respond(404, json={"code": "E_ROUTE_NOT_FOUND"})

    with pytest.raises(RouteError):
        await client.opciones()

    assert route.call_count == 1


async def test_network_failure_on_a_read_is_retried(
    api: respx.MockRouter, client: AswClient
) -> None:
    route = api.post("opciones").mock(side_effect=httpx.ReadTimeout("slow"))

    with pytest.raises(NetworkError) as excinfo:
        await client.opciones()

    assert excinfo.value.cause == "ReadTimeout"
    assert excinfo.value.maybe_delivered is True
    assert route.call_count == 3


@pytest.mark.parametrize(
    ("response", "detail"),
    [
        (httpx.Response(200, text="<html>ok</html>"), "not JSON"),
        (httpx.Response(418, json={"teapot": True}), "HTTP 418"),
        (httpx.Response(200, json={"not": "a list"}), "JSON array"),
    ],
)
async def test_responses_outside_the_spec_are_reported(
    api: respx.MockRouter, client: AswClient, response: httpx.Response, detail: str
) -> None:
    api.post("opciones").mock(return_value=response)

    with pytest.raises(UnexpectedResponseError, match=detail):
        await client.opciones()


async def test_schema_mismatch_does_not_echo_the_received_values(
    api: respx.MockRouter, client: AswClient
) -> None:
    api.post("ver_disponibilidad").respond(
        json={"tema": {"id": "PRIVATE-VALUE"}, "horarios": "PRIVATE-VALUE"}
    )
    api.post("sede").respond(json=[1, 2])

    with pytest.raises(UnexpectedResponseError) as excinfo:
        await client.ver_disponibilidad()
    with pytest.raises(UnexpectedResponseError):
        await client.sede()

    assert "tema.id" in str(excinfo.value)
    assert "PRIVATE-VALUE" not in str(excinfo.value)
    assert excinfo.value.__cause__ is None


# --- booking ---------------------------------------------------------------


async def test_agendar_sends_exactly_what_the_app_sends(
    api: respx.MockRouter, client: AswClient
) -> None:
    route = api.post("agendar").respond(
        json={"fecha": "2026-11-10", "hora": "06:00:00", "mensaje": "Programación exitosa"}
    )

    result = await client.agendar(DIA, HORA, "regular")

    assert sent_json(route) == {
        "zona_horaria": "America/Bogota",
        "tema_id": None,
        "hora": "06:00:00",
        "dia": "2026-11-10",
        "tipo_clase": "regular",
        "tipo_solicitud": "normal",
    }
    assert result.mensaje == "Programación exitosa"


async def test_agendar_segunda_opcion_sends_exactly_what_the_app_sends(
    api: respx.MockRouter, client: AswClient
) -> None:
    route = api.post("agendar").respond(json={"fecha": "2026-11-11", "hora": "06:00:00"})

    await client.agendar_segunda_opcion(2563, dt.date(2026, 11, 11), HORA)

    assert sent_json(route) == {
        "tema_id": 2563,
        "hora": "06:00:00",
        "dia": "2026-11-11",
        "tipo_clase": None,
        "tipo_solicitud": "normal",
        "segunda_opcion": True,
    }


async def test_cancelar_sends_the_booking_id(api: respx.MockRouter, client: AswClient) -> None:
    route = api.post("cancelar").respond(json={"ok": True})

    await client.cancelar(123)

    assert sent_json(route) == {"agendamiento_id": 123}


async def test_agendar_business_error_is_raised_as_is(
    api: respx.MockRouter, client: AswClient
) -> None:
    route = api.post("agendar").respond(500, json={"error": "No hay cupos disponibles"})

    with pytest.raises(BusinessError, match="cupos"):
        await client.agendar(DIA, HORA, "regular")

    assert route.call_count == 1


@pytest.mark.parametrize(
    "failure",
    [httpx.Response(502, text="Bad Gateway"), httpx.ReadTimeout("slow")],
    ids=["502", "read-timeout"],
)
async def test_agendar_is_never_repeated_when_it_may_have_been_processed(
    api: respx.MockRouter, client: AswClient, failure: httpx.Response | Exception
) -> None:
    route = api.post("agendar").mock(side_effect=[failure])

    with pytest.raises((ServerError, NetworkError)):
        await client.agendar(DIA, HORA, "regular")

    assert route.call_count == 1


async def test_agendar_is_retried_when_the_connection_never_opened(
    api: respx.MockRouter, client: AswClient
) -> None:
    route = api.post("agendar").mock(
        side_effect=[httpx.ConnectError("refused"), httpx.Response(200, json={})]
    )

    await client.agendar(DIA, HORA, "regular")

    assert route.call_count == 2


async def test_agendar_relogs_in_on_401(api: respx.MockRouter, client: AswClient) -> None:
    route = api.post("agendar").mock(
        side_effect=[httpx.Response(401, json=INVALID_TOKEN_401), httpx.Response(200, json={})]
    )
    api.post("login").respond(json={"token": NEW_TOKEN})

    await client.agendar(DIA, HORA, "regular")

    assert route.call_count == 2
    assert bearer(route) == f"Bearer {NEW_TOKEN}"


# --- dry-run guard ---------------------------------------------------------


async def test_dry_run_blocks_every_write_before_anything_is_sent(
    api: respx.MockRouter, store: MemoryTokenStore
) -> None:
    api.post("agendar").respond(json={})
    api.post("cancelar").respond(json={})
    api.post("login").respond(json={"token": NEW_TOKEN})

    async with AswClient(make_settings(dry_run=True), store) as client:
        with pytest.raises(DryRunError):
            await client.agendar(DIA, HORA, "regular")
        with pytest.raises(DryRunError):
            await client.agendar_segunda_opcion(2563, DIA, HORA)
        with pytest.raises(DryRunError):
            await client.cancelar(123)

    assert api.calls.call_count == 0


async def test_dry_run_still_allows_reads(api: respx.MockRouter, store: MemoryTokenStore) -> None:
    api.post("clases").respond(json=[])

    async with AswClient(make_settings(dry_run=True), store) as client:
        assert await client.clases() == []


# --- secrets ---------------------------------------------------------------


async def test_token_and_password_never_appear_in_logs_or_errors(
    api: respx.MockRouter, caplog: pytest.LogCaptureFixture
) -> None:
    """Drive every code path that handles secrets and search all output for them."""
    caplog.set_level(logging.DEBUG)
    api.post("login").respond(json={"token": NEW_TOKEN, "user": {"email": EMAIL}})
    api.post("clases").mock(
        side_effect=[httpx.Response(401, json=INVALID_TOKEN_401), httpx.Response(200, json=[])]
    )
    api.post("opciones").mock(side_effect=httpx.ConnectError("refused"))
    api.post("sede").mock(return_value=HTML_500)
    api.post("ver_disponibilidad").respond(500, json={"error": "No tiene más temas pendientes."})
    api.post("agendar").respond(401, json=INVALID_TOKEN_401)
    errors: list[BaseException] = []

    with structlog.testing.capture_logs() as events:
        async with AswClient(
            make_settings(), MemoryTokenStore(OLD_TOKEN), retry_wait_s=0
        ) as client:
            await client.clases()  # 401 -> login -> retry
            for call in (
                client.opciones(),
                client.sede(),
                client.ver_disponibilidad(),
                client.agendar(DIA, HORA, "regular"),
            ):
                with pytest.raises(Exception) as excinfo:  # noqa: PT011
                    await call
                errors.append(excinfo.value)
            await client.login()
            client_repr = repr(client)

    assert {e["event"] for e in events} >= {"http_request", "http_error", "login_ok"}
    output = "\n".join(
        [
            json.dumps(events, default=str),
            caplog.text,
            client_repr,
            *(f"{error!r} {error} {error.__cause__!r}" for error in errors),
        ]
    )
    for secret in (OLD_TOKEN, NEW_TOKEN, PASSWORD):
        assert secret not in output
    # The login log lists field names only, so we can document the response later.
    login_event = next(e for e in events if e["event"] == "login_ok")
    assert login_event["fields"] == ["token", "user"]


async def test_each_request_is_logged_with_its_duration(
    api: respx.MockRouter, client: AswClient
) -> None:
    api.post("opciones").respond(
        json=load_fixture("opciones.json"), headers={"Date": "Mon, 09 Nov 2026 18:52:00 GMT"}
    )

    with structlog.testing.capture_logs() as events:
        await client.opciones()

    (event,) = [e for e in events if e["event"] == "http_request"]
    assert event["endpoint"] == "opciones"
    assert event["status"] == 200
    assert event["attempt"] == 1
    assert event["server_date"] == "Mon, 09 Nov 2026 18:52:00 GMT"
    assert isinstance(event["duration_ms"], float)
    assert event["duration_ms"] >= 0
