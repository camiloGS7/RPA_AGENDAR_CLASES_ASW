"""Pydantic models for the ASW API responses (spec section 3.4).

Field names mirror the API (Spanish); behaviour is named in English.

Two rules shape these models:

* ``extra="allow"``: a new field on the server must never break the bot.
* Fields we have verified are typed precisely. Fields we only read from the
  app's JavaScript, or never saw in a real response, are ``Any`` on purpose:
  a wrong guess about their type would turn a successful booking into a crash.
"""

import datetime as dt
import re
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict

BOGOTA = ZoneInfo("America/Bogota")

_FECHA_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_HORA_RE = re.compile(r"\d{2}:\d{2}:\d{2}")


def _strict_fecha(value: object) -> object:
    if isinstance(value, str) and not _FECHA_RE.fullmatch(value):
        raise ValueError("fecha must be YYYY-MM-DD")
    return value


def _strict_hora(value: object) -> object:
    if isinstance(value, str) and not _HORA_RE.fullmatch(value):
        raise ValueError("hora must be HH:MM:SS")
    return value


def _assume_bogota(value: dt.datetime) -> dt.datetime:
    """The API sends local datetimes without a zone; they are Bogota time."""
    if value.tzinfo is None:
        return value.replace(tzinfo=BOGOTA)
    return value.astimezone(BOGOTA)


def _none_as_zero(value: object) -> object:
    return 0 if value is None else value


Fecha = Annotated[dt.date, BeforeValidator(_strict_fecha)]
HoraDelDia = Annotated[dt.time, BeforeValidator(_strict_hora)]
BogotaDateTime = Annotated[dt.datetime, AfterValidator(_assume_bogota)]


class ApiModel(BaseModel):
    """Base for every API payload: tolerant to new fields, immutable once parsed."""

    model_config = ConfigDict(extra="allow", frozen=True)


class Tema(ApiModel):
    """The topic the student must take next. Advances after each booking."""

    id: int
    curso_id: int | None = None
    nombre: str | None = None
    orden: int | None = None
    tipo_clase: str | None = None
    tipo: str | None = None
    programable: int | None = None
    activo: int | None = None
    unidad: str | None = None
    class_topic: str | None = None


class Hora(ApiModel):
    """One 1.5 h block of a day, with its free seats."""

    id: int
    hora: HoraDelDia
    nombre: str | None = None
    nombre_corto: str | None = None
    marca_tiempo: BogotaDateTime | None = None
    timestamp: int | None = None
    politica_cancel: str | None = None
    disponible: Annotated[int, BeforeValidator(_none_as_zero)] = 0
    mensaje_cierre: Any = None

    @property
    def has_seats(self) -> bool:
        return self.disponible > 0 and not self.mensaje_cierre


class Dia(ApiModel):
    """One schedulable day and its blocks."""

    fecha: Fecha
    texto: str | None = None
    dia_semana: int | None = None
    timestamp: int | None = None
    date: dt.datetime | None = None
    horas: tuple[Hora, ...] = ()

    @property
    def weekday(self) -> int:
        """ISO weekday (1=Monday ... 7=Sunday), computed from ``fecha``.

        We do not trust ``dia_semana``: we do not know what the API sends on Sundays.
        """
        return self.fecha.isoweekday()

    def slot(self, hora: dt.time) -> Hora | None:
        """Return the block that starts at ``hora``, if the day has it."""
        return next((h for h in self.horas if h.hora == hora), None)

    def starts_at(self, hora: dt.time) -> dt.datetime:
        """Timezone-aware start of a block on this day."""
        return dt.datetime.combine(self.fecha, hora, tzinfo=BOGOTA)


class Disponibilidad(ApiModel):
    """Response of ``ver_disponibilidad``: next topic plus the visible days."""

    tema: Tema
    horarios: tuple[Dia, ...] = ()

    @property
    def fechas(self) -> frozenset[dt.date]:
        return frozenset(d.fecha for d in self.horarios)

    def day(self, fecha: dt.date) -> Dia | None:
        """Look a day up by date. The API does not sort days, so never index."""
        return next((d for d in self.horarios if d.fecha == fecha), None)


class MasOpciones(ApiModel):
    """Response of ``ver_mas_opciones``: same hour on the next working day.

    Unlike ``ver_disponibilidad``, ``horarios`` is a single day here, not a list.
    """

    tema: Tema
    horarios: Dia


class Opcion(ApiModel):
    """One entry of ``opciones``. Scheduling is locked from min_date to max_date."""

    text: str
    value: str
    predeterminada: bool = False
    min_date: BogotaDateTime
    max_date: BogotaDateTime


class Reserva(ApiModel):
    """One of my bookings (``clases``).

    Field list read from the app's JavaScript; types are not verified yet, so
    everything except ``id`` stays loose until phase 6 captures a real one.
    """

    id: int
    fecha: Any = None
    hora: Any = None
    fecha_bloque: Any = None
    estado: Any = None
    tema: Any = None
    tema_id: Any = None
    tipo_clase: Any = None
    tipo_solicitud: Any = None
    sede: Any = None
    sede_id: Any = None
    politica_cancel_tiempo: Any = None
    politica_cancel_mensaje: Any = None
    link_activo: Any = None
    asistencia_fecha: Any = None
    observaciones: Any = None

    @property
    def booked_date(self) -> dt.date | None:
        """Best-effort date of the booking, or None if ``fecha`` is not date-like."""
        if not isinstance(self.fecha, str):
            return None
        try:
            return dt.date.fromisoformat(self.fecha[:10])
        except ValueError:
            return None

    @property
    def booked_time(self) -> dt.time | None:
        """Best-effort start time of the booking, or None if ``hora`` is not time-like."""
        if not isinstance(self.hora, str):
            return None
        try:
            return dt.time.fromisoformat(self.hora[:8])
        except ValueError:
            return None


class AgendarResult(ApiModel):
    """Success body of ``agendar``. Format deduced, not yet captured for real."""

    fecha: Any = None
    hora: Any = None
    mensaje: Any = None
