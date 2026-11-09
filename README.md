# asw-bot

Bot en Python que agenda automáticamente clases de inglés en la plataforma
Blocks de American School Way (ASW) cuando se abre la programación diaria.

Estado: **Fase 0 (andamiaje)**. Todavía no hace ninguna petición a la API.

## Requisitos

- [uv](https://docs.astral.sh/uv/) (instala Python 3.12 por su cuenta)
- Git

## Puesta en marcha

```bash
uv sync
```

```bash
cp .env.example .env
```

Después edita `.env` con tus credenciales. Ese archivo está en `.gitignore`
y nunca debe subirse al repositorio.

## Comandos de calidad

| Comando | Qué revisa |
|---|---|
| `uv run ruff check .` | Lint |
| `uv run ruff format --check .` | Formato |
| `uv run mypy` | Tipos (modo estricto sobre `src/`) |
| `uv run pytest` | Pruebas y cobertura (mínimo 85 %) |
| `uv run pytest -m live` | Pruebas de solo lectura contra la API real |
| `uv run python scripts/show_tree.py` | Árbol del proyecto |

## Estructura

```
src/asw_bot/     código del bot
  strategy.py    lógica pura: qué reservar dada una disponibilidad
  client.py      entrada/salida: peticiones HTTP a la API
  runner.py      orquestación de la ventana de apertura
mock_server/     servidor FastAPI que imita a ASW para las pruebas
tests/           pruebas (fixtures/ guarda respuestas reales anonimizadas)
reports/         reportes HTML generados (no se versionan)
```

## Límites de seguridad

- Las pruebas y el modo `--dry-run` nunca llaman a `agendar` ni a `cancelar`
  en la API real.
- El token y la contraseña no se imprimen, no se loguean y no se versionan.
- Como máximo una consulta cada 400 ms, y solo dentro de la ventana de apertura.
