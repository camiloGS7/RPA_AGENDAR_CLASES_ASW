"""The mock must behave like the ASW API the spec documents, or the e2e tests prove nothing."""

import datetime as dt
from typing import Any

import pytest
from fastapi.testclient import TestClient
from mock_server.app import (
    API_PREFIX,
    DEFAULT_SEATS,
    Fault,
    MockAsw,
    MockConfig,
    _label,
    create_app,
    create_app_from_env,
    decode_token,
    next_working_day,
    sign_token,
)

from asw_bot.clock import ManualClock
from asw_bot.models import BOGOTA, Disponibilidad, MasOpciones, Opcion, Reserva

T = dt.datetime(2026, 11, 10, 4, 40, tzinfo=BOGOTA)  # a Tuesday
TUE = dt.date(2026, 11, 10)
WED = dt.date(2026, 11, 11)


class Harness:
    """A mock server, its clock, and a logged-in HTTP client."""

    def __init__(self, *, seconds_after_opening: float = 1, **config: Any) -> None:
        self.clock = ManualClock(T + dt.timedelta(seconds=seconds_after_opening))
        self.app = create_app(MockConfig(open_at=T, clock=self.clock.now, **config))
        self.state: MockAsw = self.app.state.mock
        self.http = TestClient(self.app, base_url="http://mock.test")
        self.token = self.http.post(f"{API_PREFIX}/login", json={"user": {}}).json()["token"]

    def post(self, endpoint: str, body: Any = None, *, token: str | None = "") -> Any:
        token = self.token if token == "" else token
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return self.http.post(f"{API_PREFIX}/{endpoint}", json=body, headers=headers)

    def book(self, hora: str, day: dt.date = WED) -> Any:
        return self.post(
            "agendar", {"dia": day.isoformat(), "hora": hora, "tipo_solicitud": "normal"}
        )


# --- conventions -----------------------------------------------------------


def test_only_post_exists() -> None:
    mock = Harness()

    response = mock.http.get(
        f"{API_PREFIX}/opciones", headers={"Authorization": f"Bearer {mock.token}"}
    )

    assert response.status_code == 404
    assert response.json()["code"] == "E_ROUTE_NOT_FOUND"
    assert mock.post("no_existe").status_code == 404


@pytest.mark.parametrize(
    ("token", "message"),
    [
        (None, "E_INVALID_JWT_TOKEN: jwt must be provided"),
        ("garbage", "E_INVALID_JWT_TOKEN: invalid token"),
        ("a.b.c", "E_INVALID_JWT_TOKEN: invalid token"),
        (sign_token({"uid": 1}, key="another-key"), "E_INVALID_JWT_TOKEN: invalid signature"),
    ],
)
def test_auth_failures_use_the_adonis_body(token: str | None, message: str) -> None:
    response = Harness().post("opciones", {}, token=token)

    assert response.status_code == 401
    assert response.json() == {
        "message": message,
        "name": "InvalidJwtToken",
        "code": "E_INVALID_JWT_TOKEN",
        "status": 401,
    }


def test_token_round_trip_and_revocation() -> None:
    mock = Harness()
    payload, reason = decode_token(mock.token)

    assert reason is None
    assert payload is not None
    assert set(payload) == {"uid", "data", "iat"}  # like the real token: no "exp"
    assert mock.post("clases", {}).status_code == 200

    mock.state.revoke_tokens()
    assert mock.post("clases", {}).status_code == 401


def test_login_checks_credentials_when_configured() -> None:
    mock = Harness(email="a@b.co", password="right")
    url = f"{API_PREFIX}/login"

    ok = mock.http.post(url, json={"user": {"email": "a@b.co", "password": "right"}})
    bad = mock.http.post(url, json={"user": {"email": "a@b.co", "password": "wrong"}})

    assert ok.status_code == 200
    assert bad.status_code == 401
    # Not even fake credentials are kept in the request log.
    assert all(r.body == {} for r in mock.state.requests if r.endpoint == "login")


def test_responses_carry_date_and_server_headers() -> None:
    response = Harness().post("opciones", {})

    assert response.headers["server"] == "nginx"
    assert response.headers["date"] == "Tue, 10 Nov 2026 09:40:01 GMT"


# --- lock and opening --------------------------------------------------------


def test_opciones_publishes_the_opening_time() -> None:
    opciones = [Opcion.model_validate(o) for o in Harness().post("opciones", {}).json()]

    assert [o.value for o in opciones] == ["clase", "tutoria"]
    assert opciones[0].max_date == T
    assert opciones[0].min_date == T.replace(hour=0, minute=0)


def test_before_opening_everything_is_locked() -> None:
    mock = Harness(seconds_after_opening=-1)

    for response in (mock.post("ver_disponibilidad"), mock.book("07:30:00")):
        assert response.status_code == 500
        assert response.json() == {"error": "Programación bloqueada"}


def test_lock_response_is_configurable() -> None:
    mock = Harness(seconds_after_opening=-1, locked_status=403, locked_message="Cerrado")

    response = mock.post("ver_disponibilidad")

    assert (response.status_code, response.json()) == (403, {"error": "Cerrado"})


def test_stale_lock_shows_only_the_known_day_and_still_refuses_bookings() -> None:
    mock = Harness(seconds_after_opening=-1, locked_mode="stale")

    disponibilidad = Disponibilidad.model_validate(mock.post("ver_disponibilidad").json())

    assert disponibilidad.fechas == {TUE}
    assert mock.book("07:30:00", TUE).status_code == 500


def test_after_opening_a_new_day_appears_first_and_unsorted() -> None:
    disponibilidad = Disponibilidad.model_validate(Harness().post("ver_disponibilidad").json())

    assert [d.fecha for d in disponibilidad.horarios] == [WED, TUE]
    new_day = disponibilidad.day(WED)
    assert new_day is not None
    assert [h.disponible for h in new_day.horas] == list(DEFAULT_SEATS.values())
    assert new_day.texto == "Miércoles 11 de Noviembre"
    assert new_day.dia_semana == 3
    assert new_day.horas[0].nombre == "6:00 am - 7:30 am"
    assert new_day.horas[0].marca_tiempo == dt.datetime(2026, 11, 11, 6, 0, tzinfo=BOGOTA)
    assert disponibilidad.tema.id == 2563


def test_today_only_lists_blocks_at_least_one_hour_away() -> None:
    mock = Harness(seconds_after_opening=3 * 3600)  # 07:40

    today = Disponibilidad.model_validate(mock.post("ver_disponibilidad").json()).day(TUE)

    assert today is not None
    assert today.horas[0].hora == dt.time(9, 0)
    assert today.horas[0].politica_cancel == "Esta clase ya no es posible cancelarla."


def test_competitors_drain_seats_over_time() -> None:
    mock = Harness(seconds_after_opening=0, drain_per_tick={"07:30:00": 2})

    def seats() -> int:
        day = Disponibilidad.model_validate(mock.post("ver_disponibilidad").json()).day(WED)
        assert day is not None
        slot = day.slot(dt.time(7, 30))
        assert slot is not None
        return slot.disponible

    assert seats() == 15
    mock.clock.advance(0.45)  # two ticks of 200 ms
    assert seats() == 11
    mock.clock.advance(10)
    assert seats() == 0


# --- booking rules -----------------------------------------------------------


def test_booking_takes_a_seat_advances_the_topic_and_is_listed() -> None:
    mock = Harness()

    response = mock.book("07:30:00")

    assert response.status_code == 200
    assert response.json() == {
        "fecha": "2026-11-11",
        "hora": "07:30:00",
        "mensaje": "Programación exitosa para Miércoles 11 de Noviembre 7:30am",
    }
    after = Disponibilidad.model_validate(mock.post("ver_disponibilidad").json())
    day = after.day(WED)
    assert day is not None
    slot = day.slot(dt.time(7, 30))
    assert slot is not None
    assert slot.disponible == 14
    assert after.tema.id == 2564
    (reserva,) = [Reserva.model_validate(c) for c in mock.post("clases", {}).json()]
    assert (reserva.booked_date, reserva.booked_time) == (WED, dt.time(7, 30))
    assert reserva.tema_id == 2563


@pytest.mark.parametrize(
    ("config", "body", "fragment"),
    [
        (
            {"new_day_seats": {"07:30:00": 0}},
            {"dia": "2026-11-11", "hora": "07:30:00"},
            "No hay cupos",
        ),
        ({}, {"dia": "2026-11-11", "hora": "08:00:00"}, "Horario no disponible"),
        ({}, {"dia": "2026-11-20", "hora": "07:30:00"}, "Horario no disponible"),
        ({}, {"dia": "mañana", "hora": "07:30:00"}, "Fecha no válida"),
        ({}, {"dia": "2026-11-10", "hora": "06:00:00"}, None),  # 04:40 -> 06:00 is allowed
        ({"pending_topics": 0}, {"dia": "2026-11-11", "hora": "07:30:00"}, "temas pendientes"),
    ],
)
def test_booking_refusals(
    config: dict[str, Any], body: dict[str, str], fragment: str | None
) -> None:
    response = Harness(**config).post("agendar", body)

    if fragment is None:
        assert response.status_code == 200
    else:
        assert response.status_code == 500
        assert fragment in response.json()["error"]


def test_one_hour_rule_same_block_and_daily_cap() -> None:
    mock = Harness(seconds_after_opening=45 * 60)  # 05:25

    assert "1 hora antes" in mock.book("06:00:00", TUE).json()["error"]
    assert mock.book("07:30:00").status_code == 200
    assert "en este horario" in mock.book("07:30:00").json()["error"]
    for hora in ("09:00:00", "10:30:00", "12:00:00", "13:30:00"):
        assert mock.book(hora).status_code == 200
    assert "máximo de clases" in mock.book("15:00:00").json()["error"]
    assert len(mock.state.bookings) == 5


def test_seats_can_vanish_right_after_being_seen() -> None:
    mock = Harness(stolen_after_view=frozenset({"07:30:00"}))

    seen = Disponibilidad.model_validate(mock.post("ver_disponibilidad").json()).day(WED)
    assert seen is not None
    slot = seen.slot(dt.time(7, 30))
    assert slot is not None
    assert slot.disponible == 15

    assert "No hay cupos" in mock.book("07:30:00").json()["error"]
    assert mock.book("09:00:00").status_code == 200


def test_cancelling_follows_the_24_hour_rule() -> None:
    mock = Harness()
    mock.book("06:00:00")  # Wednesday 06:00 is 25 h 20 min away
    mock.book("06:00:00", TUE)  # today
    wednesday, tuesday = (b["id"] for b in mock.state.bookings)

    assert mock.post("cancelar", {"agendamiento_id": tuesday}).status_code == 500
    assert mock.post("cancelar", {"agendamiento_id": wednesday}).status_code == 200
    assert mock.post("cancelar", {"agendamiento_id": 1}).status_code == 500
    assert [b["id"] for b in mock.state.bookings] == [tuesday]
    assert mock.state.seats(WED, "06:00:00", mock.state.now()) == 18


def test_mas_opciones_offers_the_same_hour_on_the_next_working_day() -> None:
    mock = Harness()
    body = {"tema_id": 2563, "fecha": "2026-11-13", "hora": "07:30:00"}  # a Friday

    mas = MasOpciones.model_validate(mock.post("ver_mas_opciones", body).json())

    assert mas.horarios.fecha == dt.date(2026, 11, 16)  # Monday
    assert [h.hora for h in mas.horarios.horas] == [dt.time(7, 30)]
    assert next_working_day(WED) == dt.date(2026, 11, 12)
    assert mock.post("ver_mas_opciones", {"fecha": "2026-11-13", "hora": "x"}).status_code == 500


def test_second_option_can_be_booked_on_the_offered_day() -> None:
    mock = Harness()
    body = {"dia": "2026-11-12", "hora": "07:30:00", "tema_id": 2563, "segunda_opcion": True}

    assert mock.post("agendar", {**body, "segunda_opcion": False}).status_code == 500
    assert mock.post("agendar", body).status_code == 200


def test_profile_endpoints() -> None:
    mock = Harness()
    assert mock.post("curso_actual", {}).json()["codigo"] == "C_EN-A2"
    assert mock.post("sede", {}).json()["nombre"] == "SUBA"


# --- fault injection ---------------------------------------------------------


def test_fault_can_drop_the_request_or_only_the_response() -> None:
    mock = Harness()
    mock.state.faults += [Fault("agendar"), Fault("agendar", status=503, process=True)]

    dropped = mock.book("07:30:00")
    lost_response = mock.book("07:30:00")

    assert (dropped.status_code, dropped.text) == (502, "Bad Gateway")
    assert lost_response.status_code == 503
    # The second one was processed: the class is booked although the client saw an error.
    assert [b["hora"] for b in mock.state.bookings] == ["07:30:00"]
    assert mock.book("09:00:00").status_code == 200


# --- helpers -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("hora", "long", "short"),
    [
        ("06:00:00", "6:00 am - 7:30 am", "6:00am"),
        ("10:30:00", "10:30 am - 12:00 pm", "10:30am"),
        ("12:00:00", "12:00 pm - 1:30 pm", "12:00pm"),
        ("16:30:00", "4:30 pm - 6:00 pm", "4:30pm"),
    ],
)
def test_block_labels_match_the_real_ones(hora: str, long: str, short: str) -> None:
    assert _label(hora) == long
    assert _label(hora, short=True) == short


def test_app_from_environment_opens_after_the_configured_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MOCK_OPEN_AFTER_SECONDS", "3600")
    monkeypatch.setenv("MOCK_LOCKED_MODE", "stale")

    state: MockAsw = create_app_from_env().state.mock

    assert state.config.locked_mode == "stale"
    assert state.config.open_at.microsecond == 0
    assert not state.is_open(state.now())
