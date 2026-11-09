"""Models parse the documented responses and reject malformed dates and hours."""

import datetime as dt

import pytest
from pydantic import ValidationError

from asw_bot.models import (
    BOGOTA,
    AgendarResult,
    Dia,
    Disponibilidad,
    Hora,
    MasOpciones,
    Opcion,
    Reserva,
)
from helpers import load_fixture

TUESDAY = dt.date(2026, 11, 10)
MONDAY = dt.date(2026, 11, 9)


@pytest.fixture
def disponibilidad() -> Disponibilidad:
    return Disponibilidad.model_validate(load_fixture("ver_disponibilidad.json"))


def test_disponibilidad_parses_topic_and_days(disponibilidad: Disponibilidad) -> None:
    assert disponibilidad.tema.id == 2563
    assert disponibilidad.tema.tipo_clase == "regular"
    assert disponibilidad.fechas == {MONDAY, TUESDAY}


def test_days_are_found_by_date_even_when_unordered(disponibilidad: Disponibilidad) -> None:
    # The fixture lists Tuesday before Monday, as the real API did.
    assert [d.fecha for d in disponibilidad.horarios] == [TUESDAY, MONDAY]

    monday = disponibilidad.day(MONDAY)
    tuesday = disponibilidad.day(TUESDAY)

    assert monday is not None
    assert tuesday is not None
    assert monday.texto == "Lunes 9 de Noviembre"
    assert [h.disponible for h in tuesday.horas] == [18, 15, 24, 34, 24, 6, 3, 41, 36, 18]
    assert disponibilidad.day(dt.date(2026, 11, 11)) is None


def test_today_only_lists_blocks_still_schedulable(disponibilidad: Disponibilidad) -> None:
    monday = disponibilidad.day(MONDAY)
    assert monday is not None
    assert [h.hora for h in monday.horas] == [
        dt.time(15, 0),
        dt.time(16, 30),
        dt.time(18, 0),
        dt.time(19, 30),
    ]


def test_slot_lookup_and_seats(disponibilidad: Disponibilidad) -> None:
    monday = disponibilidad.day(MONDAY)
    tuesday = disponibilidad.day(TUESDAY)
    assert monday is not None
    assert tuesday is not None

    full = monday.slot(dt.time(15, 0))
    free = tuesday.slot(dt.time(6, 0))

    assert full is not None
    assert not full.has_seats
    assert free is not None
    assert free.has_seats
    assert free.id == 1
    assert tuesday.slot(dt.time(5, 0)) is None


def test_weekday_comes_from_the_date_not_from_the_api_field() -> None:
    sunday = Dia.model_validate({"fecha": "2026-11-15", "dia_semana": 0, "horas": []})
    assert sunday.weekday == 7
    assert Dia.model_validate({"fecha": "2026-11-10"}).weekday == 2


def test_block_times_are_bogota_time(disponibilidad: Disponibilidad) -> None:
    tuesday = disponibilidad.day(TUESDAY)
    assert tuesday is not None
    first = tuesday.horas[0]

    assert first.marca_tiempo == dt.datetime(2026, 11, 10, 6, 0, tzinfo=BOGOTA)
    assert tuesday.starts_at(first.hora) == first.marca_tiempo
    # The API's own epoch timestamp confirms the zone: 06:00 Bogota is 11:00 UTC.
    assert first.timestamp is not None
    assert first.marca_tiempo.timestamp() * 1000 == first.timestamp


def test_opciones_dates_are_interpreted_as_bogota() -> None:
    opciones = [Opcion.model_validate(item) for item in load_fixture("opciones.json")]
    clase = next(o for o in opciones if o.value == "clase")

    assert clase.predeterminada is True
    assert clase.max_date == dt.datetime(2026, 11, 9, 4, 40, tzinfo=BOGOTA)
    assert clase.max_date.utcoffset() == dt.timedelta(hours=-5)
    assert clase.max_date.astimezone(dt.UTC) == dt.datetime(2026, 11, 9, 9, 40, tzinfo=dt.UTC)


def test_datetimes_that_carry_a_zone_are_converted_to_bogota() -> None:
    opcion = Opcion.model_validate(
        {
            "text": "Clase",
            "value": "clase",
            "min_date": "2026-11-09T05:00:00Z",
            "max_date": "2026-11-09T09:40:00Z",
        }
    )
    assert opcion.max_date == dt.datetime(2026, 11, 9, 4, 40, tzinfo=BOGOTA)
    assert opcion.max_date.utcoffset() == dt.timedelta(hours=-5)


def test_mas_opciones_has_a_single_day() -> None:
    mas = MasOpciones.model_validate(load_fixture("ver_mas_opciones.json"))

    assert mas.horarios.fecha == dt.date(2026, 11, 11)
    assert mas.horarios.horas[0].hora == dt.time(6, 0)
    assert mas.horarios.horas[0].disponible == 18


def test_unknown_fields_are_kept_not_rejected() -> None:
    hora = Hora.model_validate({"id": 1, "hora": "06:00:00", "disponible": 2, "campo_nuevo": "x"})
    assert hora.model_extra == {"campo_nuevo": "x"}


def test_closed_block_has_no_seats() -> None:
    closed = Hora.model_validate(
        {"id": 1, "hora": "06:00:00", "disponible": 5, "mensaje_cierre": "Franja cerrada"}
    )
    assert not closed.has_seats


@pytest.mark.parametrize(
    "payload", [{"id": 1, "hora": "06:00:00"}, {"id": 1, "hora": "06:00:00", "disponible": None}]
)
def test_missing_seat_count_means_zero(payload: dict[str, object]) -> None:
    hora = Hora.model_validate(payload)
    assert hora.disponible == 0
    assert not hora.has_seats


@pytest.mark.parametrize("fecha", ["10/11/2026", "2026-11-10T00:00:00", "2026-13-01", ""])
def test_malformed_fecha_is_rejected(fecha: str) -> None:
    with pytest.raises(ValidationError):
        Dia.model_validate({"fecha": fecha})


@pytest.mark.parametrize("hora", ["6:00", "06:00", "25:00:00", "06:00:00.000", ""])
def test_malformed_hora_is_rejected(hora: str) -> None:
    with pytest.raises(ValidationError):
        Hora.model_validate({"id": 1, "hora": hora})


def test_models_are_immutable(disponibilidad: Disponibilidad) -> None:
    with pytest.raises(ValidationError):
        disponibilidad.tema.id = 1  # type: ignore[misc]


@pytest.mark.parametrize(
    ("fecha", "expected"),
    [
        ("2026-11-10", dt.date(2026, 11, 10)),
        ("2026-11-10T05:00:00.000Z", dt.date(2026, 11, 10)),
        ("not a date", None),
        (None, None),
        (20261110, None),
    ],
)
def test_reserva_date_is_best_effort(fecha: object, expected: dt.date | None) -> None:
    reserva = Reserva.model_validate({"id": 99, "fecha": fecha, "estado": "programada"})
    assert reserva.id == 99
    assert reserva.booked_date == expected


@pytest.mark.parametrize(
    ("hora", "expected"),
    [
        ("06:00:00", dt.time(6, 0)),
        ("19:30", dt.time(19, 30)),
        ("06:00:00.000", dt.time(6, 0)),
        ("6am", None),
        (None, None),
        (600, None),
    ],
)
def test_reserva_time_is_best_effort(hora: object, expected: dt.time | None) -> None:
    reserva = Reserva.model_validate({"id": 99, "hora": hora})
    assert reserva.booked_time == expected


def test_agendar_result_accepts_the_deduced_shape() -> None:
    result = AgendarResult.model_validate(
        {"fecha": "2026-11-10", "hora": "06:00:00", "mensaje": "Programación exitosa"}
    )
    assert result.mensaje == "Programación exitosa"
    assert AgendarResult.model_validate({}).mensaje is None
