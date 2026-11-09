"""HTML report of one run: what was sent, when, and what came back.

Two charts share the same horizontal axis, milliseconds relative to the
opening time T, so "how long after the opening did we book?" is read directly:

* Requests: one row per endpoint, a mark where each request started and a
  thin bar for how long it took. Colour is the endpoint, shape is the result.
* Seats: what the bot saw for each preferred hour, and when it booked.

The page is self-contained (Plotly is embedded) and adapts to a dark theme.
"""

import datetime as dt
import html
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import plotly.graph_objects as go

from asw_bot.runner import RequestEvent, RunResult

# Categorical palette, fixed order (never cycled): light and dark steps of the same hues.
_SERIES = (
    ("#2a78d6", "#3987e5"),
    ("#eb6834", "#d95926"),
    ("#1baf7a", "#199e70"),
    ("#eda100", "#c98500"),
    ("#e87ba4", "#d55181"),
    ("#008300", "#008300"),
)
_INK = {
    "surface": ("#fcfcfb", "#1a1a19"),
    "page": ("#f9f9f7", "#0d0d0d"),
    "text": ("#0b0b0b", "#f0efec"),
    "text2": ("#52514e", "#c3c2b7"),
    "muted": ("#898781", "#898781"),
    "grid": ("#e1e0d9", "#2c2c2a"),
    "axis": ("#c3c2b7", "#383835"),
}
_ENDPOINT_ORDER = (
    "ver_disponibilidad",
    "agendar",
    "clases",
    "opciones",
    "login",
    "ver_mas_opciones",
)
_FONT = 'system-ui, -apple-system, "Segoe UI", sans-serif'
# How far before T the charts start by default; earlier requests are in the table.
_VIEW_BEFORE_MS = 3500.0

SeatCurves = Mapping[dt.time, Sequence[tuple[float, int]]]


def _ms(moment: dt.datetime, opening: dt.datetime) -> float:
    return round((moment - opening).total_seconds() * 1000, 1)


def _outcome(event: RequestEvent) -> tuple[str, str]:
    """(label, marker symbol) for a request's result."""
    if event.error is not None or event.status is None:
        return f"Fallo de red ({event.error})", "diamond-open"
    if event.status < 300:
        return f"{event.status} OK", "circle"
    if event.status == 401:
        return "401 token rechazado", "square-open"
    return f"{event.status} rechazada o error", "x"


def _base_layout(title: str, y_title: str, x_range: tuple[float, float]) -> dict[str, Any]:
    ink = {name: pair[0] for name, pair in _INK.items()}
    axis = {
        "gridcolor": ink["grid"],
        "linecolor": ink["axis"],
        "zeroline": False,
        "tickfont": {"color": ink["muted"], "size": 12},
        "title": {"font": {"color": ink["text2"], "size": 12}},
    }
    return {
        "title": {
            "text": title,
            "font": {"size": 15, "color": ink["text"]},
            "x": 0,
            "xanchor": "left",
        },
        "paper_bgcolor": ink["surface"],
        "plot_bgcolor": ink["surface"],
        "font": {"family": _FONT, "color": ink["text"], "size": 13},
        "margin": {"l": 150, "r": 30, "t": 70, "b": 55},
        "hovermode": "closest",
        "legend": {"orientation": "h", "y": -0.22, "x": 0, "font": {"color": ink["text2"]}},
        "xaxis": {
            **axis,
            "title": {**axis["title"], "text": "Milisegundos respecto a la apertura (T = 0)"},
            "range": list(x_range),
            "ticksuffix": " ms",
        },
        "yaxis": {**axis, "title": {**axis["title"], "text": y_title}},
    }


def _reference_lines(figure: go.Figure, result: RunResult) -> None:
    """Vertical guides for the opening and its detection."""
    ink = _INK["text2"][0]
    figure.add_vline(x=0, line={"color": ink, "width": 1.5})
    figure.add_annotation(
        x=0,
        y=1.06,
        yref="paper",
        text="T: apertura",
        showarrow=False,
        font={"color": ink, "size": 12},
        xanchor="right",
        xshift=-4,
    )
    if result.detected_at is not None and result.opening is not None:
        detected = _ms(result.detected_at, result.opening)
        figure.add_vline(x=detected, line={"color": ink, "width": 1.5, "dash": "dot"})
        figure.add_annotation(
            x=detected,
            y=1.06,
            yref="paper",
            text=f"detectada +{detected:.0f} ms",
            showarrow=False,
            font={"color": ink, "size": 12},
            xanchor="left",
            xshift=4,
        )


def requests_figure(result: RunResult, x_range: tuple[float, float]) -> go.Figure:
    """Timeline of every HTTP request: colour by endpoint, shape by result."""
    opening = result.opening
    assert opening is not None  # noqa: S101 - callers check
    figure = go.Figure()
    present = [e for e in _ENDPOINT_ORDER if any(r.endpoint == e for r in result.requests)]
    present += sorted({r.endpoint for r in result.requests} - set(present))

    for index, endpoint in enumerate(present):
        color = _SERIES[index % len(_SERIES)][0] if index < len(_SERIES) else _INK["muted"][0]
        events = [r for r in result.requests if r.endpoint == endpoint]
        starts = [_ms(r.started_at, opening) for r in events]
        # Duration bars: start -> end of each request, as one broken line.
        bar_x: list[float | None] = []
        bar_y: list[str | None] = []
        for start, event in zip(starts, events, strict=True):
            bar_x += [start, start + event.rtt_ms, None]
            bar_y += [endpoint, endpoint, None]
        figure.add_trace(
            go.Scatter(
                x=bar_x,
                y=bar_y,
                mode="lines",
                line={"color": color, "width": 2},
                hoverinfo="skip",
                showlegend=False,
            )
        )
        figure.add_trace(
            go.Scatter(
                x=starts,
                y=[endpoint] * len(events),
                mode="markers",
                name=endpoint,
                marker={
                    "color": color,
                    "size": 10,
                    "symbol": [_outcome(r)[1] for r in events],
                    "line": {"color": color, "width": 2},
                },
                customdata=[[_outcome(r)[0], r.rtt_ms] for r in events],
                hovertemplate=(
                    f"<b>{endpoint}</b><br>enviada a %{{x:.0f}} ms<br>"
                    "resultado: %{customdata[0]}<br>duración: %{customdata[1]:.0f} ms<extra></extra>"
                ),
            )
        )

    # Shape legend, in neutral ink: the shape says how the request ended.
    for label, symbol in (
        ("200 OK", "circle"),
        ("rechazada o error (5xx)", "x"),
        ("token rechazado (401)", "square-open"),
        ("fallo de red", "diamond-open"),
    ):
        figure.add_trace(
            go.Scatter(
                x=[None],
                y=[None],
                mode="markers",
                name=label,
                marker={
                    "color": _INK["muted"][0],
                    "size": 10,
                    "symbol": symbol,
                    "line": {"width": 2, "color": _INK["muted"][0]},
                },
                hoverinfo="skip",
            )
        )

    layout = _base_layout("Peticiones enviadas", "", x_range)
    layout["yaxis"].update({"categoryorder": "array", "categoryarray": list(reversed(present))})
    layout["height"] = 150 + 48 * max(len(present), 2)
    figure.update_layout(**layout)
    _reference_lines(figure, result)
    return figure


def seats_figure(
    result: RunResult, x_range: tuple[float, float], competitor: SeatCurves | None = None
) -> go.Figure:
    """Seats of the preferred hours as the bot saw them, with each booking marked."""
    opening = result.opening
    assert opening is not None  # noqa: S101 - callers check
    figure = go.Figure()
    target = result.target_date
    observations = [o for o in result.observations if o.fecha == target]
    hours = sorted({hora for o in observations for hora in o.seats})[: len(_SERIES)]

    for index, hora in enumerate(hours):
        color = _SERIES[index][0]
        label = f"{hora:%H:%M}"
        if competitor is not None and hora in competitor:
            curve = competitor[hora]
            figure.add_trace(
                go.Scatter(
                    x=[x for x, _ in curve],
                    y=[y for _, y in curve],
                    mode="lines",
                    name=f"{label} en el servidor",
                    line={"color": color, "width": 1.5, "dash": "dot", "shape": "hv"},
                    hovertemplate=f"<b>{label}</b> en el servidor<br>%{{x:.0f}} ms: %{{y}} cupos<extra></extra>",
                )
            )
        seen = [(o.at, o.seats[hora]) for o in observations if hora in o.seats]
        figure.add_trace(
            go.Scatter(
                x=[_ms(at, opening) for at, _ in seen],
                y=[seats for _, seats in seen],
                mode="lines+markers",
                name=f"{label} visto por el bot",
                line={"color": color, "width": 2, "shape": "hv"},
                marker={"color": color, "size": 8},
                hovertemplate=f"<b>{label}</b> visto por el bot<br>%{{x:.0f}} ms: %{{y}} cupos<extra></extra>",
            )
        )

    ink = _INK["text"][0]
    for symbol, ok, name in (("star", True, "reserva hecha"), ("x", False, "intento fallido")):
        records = [b for b in result.bookings if b.ok is ok]
        if not records:
            continue
        ys = [_last_seen(result, b.hora, b.at) for b in records]
        figure.add_trace(
            go.Scatter(
                x=[_ms(b.at, opening) for b in records],
                y=ys,
                mode="markers+text" if ok else "markers",
                name=name,
                text=[f"{b.hora:%H:%M}" for b in records],
                textposition="top center",
                textfont={"color": _INK["text2"][0], "size": 12},
                marker={
                    "symbol": symbol,
                    "size": 14,
                    "color": ink,
                    "line": {"color": _INK["surface"][0], "width": 2},
                },
                customdata=[[f"{b.hora:%H:%M}", b.detail or ""] for b in records],
                hovertemplate=f"<b>{name}</b> %{{customdata[0]}}<br>a %{{x:.0f}} ms<br>%{{customdata[1]}}<extra></extra>",
            )
        )

    layout = _base_layout("Cupos de tus horas en el día objetivo", "Cupos libres", x_range)
    layout["yaxis"]["rangemode"] = "tozero"
    layout["height"] = 380
    figure.update_layout(**layout)
    _reference_lines(figure, result)
    return figure


def _last_seen(result: RunResult, hora: dt.time, at: dt.datetime) -> int:
    """Seats the bot last saw for ``hora`` at or before ``at`` (0 if never seen)."""
    seen = [o.seats[hora] for o in result.observations if hora in o.seats and o.at <= at]
    return seen[-1] if seen else 0


def _x_range(result: RunResult) -> tuple[float, float]:
    opening = result.opening
    assert opening is not None  # noqa: S101 - callers check
    ends = [_ms(r.started_at, opening) + r.rtt_ms for r in result.requests]
    starts = [_ms(r.started_at, opening) for r in result.requests]
    last = max(ends, default=1000.0)
    first = min(starts, default=-_VIEW_BEFORE_MS)
    return max(first - 150, -_VIEW_BEFORE_MS), last + 250


def _tiles(result: RunResult) -> list[tuple[str, str, str]]:
    """(label, value, note) for the headline numbers."""
    opening = result.opening
    tiles: list[tuple[str, str, str]] = []
    if opening is not None and result.detected_at is not None:
        tiles.append(
            ("Apertura detectada", f"+{_ms(result.detected_at, opening):.0f} ms", "después de T")
        )
    real = result.booked
    if opening is not None and real:
        tiles.append(
            ("Primera reserva confirmada", f"+{_ms(real[0].at, opening):.0f} ms", "después de T")
        )
        tiles.append(
            ("Última reserva confirmada", f"+{_ms(real[-1].at, opening):.0f} ms", "después de T")
        )
    tiles.append(
        (
            "Reservas",
            f"{len(real)} de {result.wanted}",
            "simuladas (dry-run)" if any(b.dry_run for b in real) else "hechas",
        )
    )
    if opening is not None:
        before = sum(
            1
            for r in result.requests
            if r.endpoint == "ver_disponibilidad" and r.started_at < opening
        )
        tiles.append(("Sondeos antes de T", str(before), "consultas de disponibilidad"))
    return tiles


def _bookings_table(result: RunResult) -> str:
    if not result.bookings:
        return "<p class='muted'>No hubo intentos de reserva.</p>"
    rows = []
    for b in result.bookings:
        when = f"+{_ms(b.at, result.opening):.0f} ms" if result.opening else ""
        outcome = (
            "Reservada"
            if b.ok and not b.dry_run
            else "Simulada (dry-run)"
            if b.ok
            else "No reservada"
        )
        verified = {True: "sí", False: "no", None: "-"}[b.verified]
        rows.append(
            f"<tr><td>{b.fecha.isoformat()}</td><td>{b.hora:%H:%M}</td><td>{html.escape(b.kind.value)}</td>"
            f"<td>{outcome}</td><td class='num'>{when}</td><td>{verified}</td>"
            f"<td>{html.escape(b.detail or '')}</td></tr>"
        )
    return (
        "<table><thead><tr><th>Fecha</th><th>Hora</th><th>Tipo</th><th>Resultado</th>"
        "<th class='num'>Momento</th><th>Verificada en clases</th><th>Detalle</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _requests_table(result: RunResult) -> str:
    rows = []
    for index, r in enumerate(result.requests, start=1):
        when = f"{_ms(r.started_at, result.opening):.0f}" if result.opening else ""
        rows.append(
            f"<tr><td class='num'>{index}</td><td class='num'>{when}</td><td>{html.escape(r.endpoint)}</td>"
            f"<td>{html.escape(_outcome(r)[0])}</td><td class='num'>{r.rtt_ms:.0f}</td></tr>"
        )
    return (
        "<table><thead><tr><th class='num'>#</th><th class='num'>ms desde T</th><th>Endpoint</th>"
        "<th>Resultado</th><th class='num'>Duración (ms)</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _css() -> str:
    def block(index: int) -> str:
        return "".join(f"--{name}: {pair[index]};" for name, pair in _INK.items())

    return f"""
:root {{ color-scheme: light; {block(0)} }}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{ color-scheme: dark; {block(1)} }} }}
:root[data-theme="dark"] {{ color-scheme: dark; {block(1)} }}
body {{ margin: 0; background: var(--page); color: var(--text); font-family: {_FONT}; font-size: 15px; line-height: 1.45; }}
main {{ max-width: 1100px; margin: 0 auto; padding: 24px 16px 48px; }}
h1 {{ font-size: 22px; margin: 0 0 4px; }}
h2 {{ font-size: 16px; margin: 32px 0 8px; }}
p {{ margin: 4px 0; }} .muted {{ color: var(--text2); }}
.tiles {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; margin: 20px 0; }}
.tile {{ background: var(--surface); border: 1px solid var(--grid); border-radius: 8px; padding: 12px 14px; }}
.tile .label {{ color: var(--text2); font-size: 13px; }}
.tile .value {{ font-size: 26px; font-weight: 600; margin: 2px 0; }}
.tile .note {{ color: var(--muted); font-size: 12px; }}
.chart {{ background: var(--surface); border: 1px solid var(--grid); border-radius: 8px; margin: 12px 0; padding: 4px; overflow-x: auto; }}
table {{ border-collapse: collapse; width: 100%; background: var(--surface); font-size: 14px; }}
th, td {{ text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--grid); }}
th {{ color: var(--text2); font-weight: 600; }}
.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
details {{ margin-top: 12px; }} summary {{ cursor: pointer; color: var(--text2); }}
.scroll {{ overflow-x: auto; }}
"""


def _theme_script() -> str:
    """Re-colour the charts when the page is shown in a dark theme."""
    swaps = {light: dark for light, dark in (*_SERIES, *_INK.values()) if light != dark}
    return f"""
<script>
(function () {{
  const toDark = {swaps!r};
  const toLight = Object.fromEntries(Object.entries(toDark).map(([l, d]) => [d, l]));
  function swapDeep(value, map) {{
    if (typeof value === 'string') return map[value.toLowerCase()] || value;
    if (Array.isArray(value)) return value.map(v => swapDeep(v, map));
    if (value && typeof value === 'object') {{
      const out = {{}};
      for (const key of Object.keys(value)) out[key] = swapDeep(value[key], map);
      return out;
    }}
    return value;
  }}
  let current = 'light';
  function apply() {{
    const forced = document.documentElement.dataset.theme;
    const wanted = forced || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    if (wanted === current || !window.Plotly) return;
    const map = wanted === 'dark' ? toDark : toLight;
    document.querySelectorAll('.plotly-graph-div').forEach(function (gd) {{
      Plotly.react(gd, swapDeep(gd.data, map), swapDeep(gd.layout, map));
    }});
    current = wanted;
  }}
  window.addEventListener('load', apply);
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change', apply);
  new MutationObserver(apply).observe(document.documentElement, {{ attributes: true, attributeFilter: ['data-theme'] }});
}})();
</script>
"""


def build_run_report(
    result: RunResult, *, title: str, subtitle: str = "", competitor: SeatCurves | None = None
) -> str:
    """Return the full HTML page for one run."""
    tiles = "".join(
        f"<div class='tile'><div class='label'>{html.escape(label)}</div>"
        f"<div class='value'>{html.escape(value)}</div><div class='note'>{html.escape(note)}</div></div>"
        for label, value, note in _tiles(result)
    )
    charts = ""
    if result.opening is not None and result.requests:
        x_range = _x_range(result)
        config = {"displaylogo": False, "responsive": True}
        first = requests_figure(result, x_range).to_html(
            full_html=False, include_plotlyjs="inline", config=config
        )
        second = seats_figure(result, x_range, competitor).to_html(
            full_html=False, include_plotlyjs=False, config=config
        )
        charts = (
            f"<div class='chart'>{first}</div><div class='chart'>{second}</div>"
            "<p class='muted'>Las peticiones de preparación ocurren antes del rango visible; "
            "están en la tabla de peticiones. Pasa el cursor sobre una marca para ver el detalle.</p>"
        )
    opening = result.opening.isoformat() if result.opening else "desconocida"
    return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title><style>{_css()}</style></head>
<body><main>
<h1>{html.escape(title)}</h1>
<p class="muted">{html.escape(subtitle)}</p>
<p class="muted">Apertura (T): {html.escape(opening)} · Estado: <b>{html.escape(result.status.value)}</b>
 · Motivo: {html.escape(result.reason or "-")}</p>
<div class="tiles">{tiles}</div>
{charts}
<h2>Reservas</h2><div class="scroll">{_bookings_table(result)}</div>
<details><summary>Tabla de las {len(result.requests)} peticiones</summary>
<div class="scroll">{_requests_table(result)}</div></details>
</main>{_theme_script()}</body></html>"""


def write_run_report(
    result: RunResult,
    path: Path,
    *,
    title: str,
    subtitle: str = "",
    competitor: SeatCurves | None = None,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        build_run_report(result, title=title, subtitle=subtitle, competitor=competitor),
        encoding="utf-8",
    )
    return path
