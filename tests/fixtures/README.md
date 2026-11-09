# Fixtures

Estas respuestas están **reconstruidas a partir de la especificación**, no
capturadas directamente de la API. Ninguna contiene datos personales.

| Archivo | Procedencia |
|---|---|
| `opciones.json` | Copia literal de la respuesta real citada en la especificación (3.4). |
| `ver_disponibilidad.json` | Estructura, `tema`, día martes 10 (bloque de las 06:00) y los diez cupos del martes (18, 15, 24, 34, 24, 6, 3, 41, 36, 18) son reales. El cupo 0 del lunes a las 15:00 también. |
| `ver_mas_opciones.json` | Estructura y resultado (miércoles 11, 06:00, 18 cupos) descritos en la especificación. |

## Valores completados por deducción

La especificación recorta la respuesta, así que estos valores se completaron
siguiendo el patrón del único bloque que sí aparece entero:

- `nombre`, `nombre_corto`, `marca_tiempo` y `timestamp` de los bloques 2 a 10.
- `politica_cancel` de cada bloque, según la regla de las 24 horas respecto al
  momento de la consulta (lunes 9 a las 13:52). El texto de la política de más
  de 24 horas está truncado con "…" porque así aparece en la especificación.
- Los cupos del lunes a las 16:30, 18:00 y 19:30 (2, 5 y 9) son **inventados**.
- `timestamp` y `date` del lunes y del miércoles, desplazando un día los del martes.

Cuando el observador de la fase 4 capture respuestas reales, se reemplazan
estos archivos y se elimina esta sección.
