# estudiomusica-backend

Backend FastAPI de EstudioMusica. Sustituye los almacenes JSON del frontend Next.js.

Este repositorio es el esqueleto FastAPI más las convenciones transversales de la API (errores, paginación, OpenAPI y CORS). No incluye base de datos ni lógica de negocio: Postgres + SQLAlchemy 2 async + Alembic (incluido `GET /api/v1/health/db`) llegará en otra PR.

## Requisitos

- [Python](https://www.python.org/downloads/) **3.14 o superior**. El desarrollo se fija en **3.15** (`.python-version`).
- [uv](https://docs.astral.sh/uv/) **0.13.5 o superior** (`[tool.uv] required-version`)

Si no tienes `uv`:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

`uv` descarga Python 3.15 automáticamente si no está instalado en la máquina.

## Instalación

```bash
git clone https://github.com/aserec/estudiomusica-backend.git
cd estudiomusica-backend
cp .env.example .env
uv sync --locked
```

`uv sync --locked` crea `.venv`, instala las dependencias de runtime y de desarrollo exactamente según `uv.lock` y deja el paquete `app` instalado en modo editable.

## Arrancar en local

```bash
uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

El servidor queda en [http://127.0.0.1:8000](http://127.0.0.1:8000). En desarrollo, equivalente con `fastapi-cli`:

```bash
uv run fastapi dev app/main.py
```

Host y puerto **no** se leen del `.env`: Uvicorn y `fastapi dev` usan 127.0.0.1:8000 por defecto. Cámbialos con las flags del comando:

```bash
uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8001
uv run fastapi dev app/main.py --host 127.0.0.1 --port 8001
```

Comprueba que responde:

```bash
curl http://127.0.0.1:8000/api/v1/health
```

Debe devolver HTTP 200 y:

```json
{"status":"ok"}
```

`GET /health` en la raíz es el mismo chequeo, expuesto como liveness para sondas. El contrato de la API es `GET /api/v1/health`.

Documentación interactiva (OpenAPI / Swagger UI): [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs).

## Tests, lint y types

Desde la raíz del repositorio:

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

Para formatear el código:

```bash
uv run ruff format .
```

## Convenciones de la API

`create_app()` monta un `api_router` con prefijo **`/api/v1`**. El contrato de salud es `GET /api/v1/health`; `GET /health` en la raíz es un alias de liveness.

### JSON camelCase, códigos snake_case

Las claves JSON van en camelCase (`pageSize`, `displayName`, `refreshToken`). Los **códigos de error** y los **valores de enum** se quedan en snake_case (`not_found`, `invalid_score`, `in_progress`, `multichoice_single`).

### Errores

Sobre de [aserec/estudioMusica-webapp#391](https://github.com/aserec/estudioMusica-webapp/issues/391), visto en `src/app/api/notices/route.ts`, la ruta de grading de cuestionarios y `docs/sheet-music-evaluation-contract.md` (commit `4817bcd`):

```json
{ "error": "validation", "code": "invalid_score", "fields": ["displayName"] }
```

`error` es el slug. `code` y `fields` (lista de **nombres** de campo, no objetos) son opcionales. La validación de Pydantic responde **400** `{error:"validation", fields:[...]}` y no el 422 de FastAPI.

Manejadores de dominio: `AccessDenied` → 403 `{error:"forbidden"}`, `NotFound` → 404 `{error:"not_found"}`, `Validation` → 400 `{error:"validation", fields}`, `PayloadTooLarge` → 413 `{error:"payload_too_large"}`.

`message` **no** está en el contrato del frontend. Es opcional y solo se rellena en 500 cuando `APP_ENV=development`, para no meter el traceback en el slug `error`.

### Paginación

Query params `page` (por defecto 1) y `pageSize` (por defecto 20, máximo 100). Respuesta:

```json
{
  "items": [],
  "page": 1,
  "pageSize": 20,
  "total": 0
}
```

La #391 pedía `?limit=&cursor=` → `{ items, nextCursor }`. Se mantiene `page`/`pageSize` porque la decisión del 24-10 en [#1](https://github.com/aserec/estudiomusica-backend/issues/1) es más reciente. Los listados de negocio pueden añadir claves extra (`unreadCount`, `categories`) junto a esos metadatos.

### Actualizaciones

**PUT** para actualizar. El único **PATCH** previsto es el autoguardado de intentos de cuestionario (`PATCH /courses/:courseId/quiz/:activityId/attempts/:attemptId`).

### CORS

Orígenes en `CORS_ORIGINS` (por defecto `http://localhost:3000` y `http://127.0.0.1:3000`, el Next.js local). Credenciales activadas. Métodos `GET,POST,PUT,PATCH,DELETE,OPTIONS`.

### OpenAPI y versión

`GET /docs` y `GET /openapi.json` publican el contrato (prefijo `/api/v1`, sobre de error, `page`/`pageSize`). La versión de API es **v1**: va en la URL y en la cabecera `X-API-Version: v1`. `info.version` de OpenAPI es la del paquete (`0.1.0`).

### Rutas de demostración

`/api/v1/examples` no es un recurso de negocio: sirve para ejercitar paginación, validación, PUT y 404. Las issues de cursos, usuarios, etc. reutilizarán los mismos esquemas.

## De dónde salen estas decisiones

La issue de convenciones es [aserec/estudioMusica-webapp#391](https://github.com/aserec/estudioMusica-webapp/issues/391) (texto completo; commit del frontend `4817bcd`). Donde choca con las **decisiones del 24-10-2026** en [aserec/estudiomusica-backend#1](https://github.com/aserec/estudiomusica-backend/issues/1), priman las de #1.

| Decisión | Dónde se vio |
| --- | --- |
| Sobre `{ error, code?, fields?: string[] }`; slugs `unauthorized` / `invalid` / `not_found` / `forbidden`; 413 | #391 y `src/app/api/notices/route.ts`, grading de cuestionarios, `docs/sheet-music-evaluation-contract.md` |
| Validación Pydantic → 400 `{error:"validation", fields:[...]}` | Criterio de aceptación de la #391 |
| `AccessDenied`→403, `NotFound`→404, `Validation`→400, `PayloadTooLarge`→413 | Alcance de la #391 |
| Prefijo `/api/v1`, OpenAPI publicado, CORS con credenciales | #391 |
| Claves JSON camelCase; códigos/enums snake_case; PUT salvo PATCH de #62 | Comentarios de @aserec en #1 (24 oct 2026) |
| Paginación `page`/`pageSize` + `total` (no `limit`/`cursor`) | #1 (24 oct 2026), más reciente que la #391 (`?limit=&cursor=` → `{items, nextCursor}`) |

## Estructura de carpetas

```text
app/
  main.py            # Factoría FastAPI: CORS, handlers, api_router /api/v1
  errors.py          # Sobre de error y handlers (validación, HTTP, 500)
  exceptions.py      # AccessDenied, NotFound, Validation, PayloadTooLarge
  config/            # Ajustes por variables de entorno (pydantic-settings)
  routers/           # Rutas HTTP (health y examples; health también en la raíz)
  schemas/           # Modelos Pydantic (camelCase, errores, paginación)
  services/          # Lógica de aplicación (salud + lista fija de examples)
  models/            # Reservado para modelos SQLAlchemy
tests/               # Tests de pytest por convención
.env.example         # Plantilla de variables de entorno
pyproject.toml       # Dependencias, ruff, pytest y mypy
uv.lock              # Lockfile reproducible de uv
.python-version      # Python 3.15 (desarrollo)
```

`GET /api/v1/health/db` se añadirá cuando exista la base de datos.

## Versiones fijadas

| Pieza | Versión |
| --- | --- |
| Python | >=3.14 (desarrollo en 3.15) |
| uv | >=0.13.5 |
| FastAPI | 0.162.0 |
| uvicorn | 0.54.0 |
| pydantic-settings | 2.16.1 |
| pytest | 9.1.1 |
| ruff | 0.17.2 |
| mypy | 2.4.0 |

Las transitivas quedan ancladas en `uv.lock`. Se eligió [uv](https://docs.astral.sh/uv/) porque instala Python, resuelve dependencias de forma reproducible y es el gestor que recomienda el ecosistema actual de FastAPI / Astral. El extra `fastapi[standard]` no se usa en runtime: arrastra `fastapi-cloud-cli`, Sentry y OpenTelemetry. En producción van `fastapi` + `uvicorn[standard]`; `fastapi-cli` y `httpx2` (el cliente que pide Starlette 1.x para `TestClient`) quedan en el grupo de desarrollo.
