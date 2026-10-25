# estudiomusica-backend

Backend FastAPI de EstudioMusica. Sustituye los almacenes JSON del frontend Next.js.

Este repositorio es el esqueleto FastAPI, las convenciones transversales de la API (errores, paginación, OpenAPI y CORS) y la persistencia: Postgres, SQLAlchemy 2 async y Alembic. No incluye modelos de negocio.

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

La sonda de Postgres es `GET /api/v1/health/db` (también hay alias en `GET /health/db`):

```bash
curl -i http://127.0.0.1:8000/api/v1/health/db
```

Con la BD disponible responde HTTP 200 y `{"db":"ok"}`. Si Postgres no responde o la sonda supera `DB_HEALTH_TIMEOUT` (2 s por defecto), HTTP 503 y `{"db":"down"}`, sin credenciales ni el sobre `{error, code, fields}` de las rutas de negocio. `GET /health/db` en la raíz es el alias de sonda; no aparece en OpenAPI.

Documentación interactiva (OpenAPI / Swagger UI): [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs).

## Postgres, migraciones y tests con BD

El DSN se lee de `DATABASE_URL` (pydantic-settings). El valor por defecto coincide con `docker-compose.yml`:

```text
postgresql+asyncpg://estudiomusica:estudiomusica@127.0.0.1:5432/estudiomusica
```

### Arrancar Postgres en local

```bash
docker compose up -d
```

Eso levanta Postgres 17 en el puerto `5432` y crea dos bases: `estudiomusica` (desarrollo) y `estudiomusica_test` (pytest). Espera a que el healthcheck esté sano:

```bash
docker compose ps
```

Aplica las migraciones sobre la base de desarrollo:

```bash
uv run alembic upgrade head
```

Para revertirlas del todo:

```bash
uv run alembic downgrade base
```

Las revisiones viven en `alembic/versions/`. La inicial solo instala `pgcrypto` (`gen_random_uuid`); las tablas de negocio irán en issues posteriores. Nueva revisión, cuando exista un modelo:

```bash
uv run alembic revision -m "descripcion" --autogenerate
```

Los modelos de negocio heredan `PersistentModel` (`app/models/base.py`): `id` UUID, `created_at`, `updated_at` y borrado lógico `deleted_at`.

### Tests con base aislada

Los tests de persistencia **no** usan los JSON de `.data/` del frontend. Usan `estudiomusica_test` (y `estudiomusica_alembic_test` solo para upgrade/downgrade de Alembic) y envuelven cada caso en una transacción que se revierte al terminar. El nombre de la base de `TEST_DATABASE_URL` debe terminar en `_test`; si no, pytest aborta.

```bash
docker compose up -d
uv run pytest
```

Si Postgres no está en `127.0.0.1:5432`, apunta `TEST_DATABASE_URL` a tu DSN async:

```bash
export TEST_DATABASE_URL=postgresql+asyncpg://estudiomusica:estudiomusica@127.0.0.1:5432/estudiomusica_test
uv run pytest
```

`uv run pytest` sin Postgres en marcha **salta** los tests de BD (mensaje sin contraseña) y sigue pasando el resto. Los tests de convenciones (errores, paginación, CORS, `/health`) no necesitan la base.

## Tests, lint y types

Desde la raíz del repositorio:

```bash
uv run pytest
uv run pytest -W error
APP_ENV=prod uv run pytest
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

`error` es el slug. `code` y `fields` (lista de **nombres** de campo, no objetos) son opcionales. La validación de Pydantic responde **400** `{error:"validation", fields:[...]}` y no el 422 de FastAPI. `fields` se sanitiza (máximo 20 claves, 64 caracteres, identificadores seguros).

Manejadores de dominio: `AccessDenied` → 403 `{error:"forbidden"}`, `NotFound` → 404 `{error:"not_found"}`, `Validation` → 400 `{error:"validation", fields}`, `PayloadTooLarge` → 413 `{error:"payload_too_large"}`.

`HTTPException(400, "texto")` responde `{error:"bad_request"}`. Un `HTTPException(422)` se convierte en 400. El `message` de un `detail` dict **no** se expone salvo `EXPOSE_ERRORS=true`.

`message` **no** está en el contrato del frontend. Es opt-in (`EXPOSE_ERRORS=false` por defecto) y no depende de `APP_ENV`. Así un 500 en development no filtra `str(exc)` por accidente.

Los 500 los captura un middleware ASGI **dentro** de CORS (`ApiErrorMiddleware`), no el `ServerErrorMiddleware` de Starlette: el sobre JSON lleva `X-API-Version` y cabeceras CORS.

El cuerpo tiene un tope (`MAX_BODY_BYTES`, 1 MiB por defecto). Un `Content-Length` por encima se rechaza **antes** de leer; en chunked se cuentan los bytes. Ambos casos responden 413 `{error:"payload_too_large"}`.

### Paginación

Query params `page` (por defecto 1, máximo `PAGINATION_MAX_PAGE` = 10000) y `pageSize` (por defecto 20, máximo `PAGINATION_MAX_PAGE_SIZE` = 100). El tope de `page` evita un `OFFSET` enorme. Los límites viven en settings; no se duplican con `Query(le=...)`. Respuesta:

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

Orígenes en `CORS_ORIGINS` (por defecto `http://localhost:3000` y `http://127.0.0.1:3000`, el Next.js local). Credenciales activadas. Métodos `GET,POST,PUT,PATCH,DELETE,OPTIONS`. Se expone `X-API-Version`. Con credenciales, `CORS_ORIGINS='*'` o un origen que no sea `http(s)://` se rechazan al arrancar. Un preflight de origen desconocido responde 403 JSON (`{error:"forbidden"}`), no `text/plain`.

### OpenAPI y versión

`GET /docs` y `GET /openapi.json` publican el contrato (prefijo `/api/v1`, sobre de error, `page`/`pageSize`). La versión de API es **v1**: va en la URL y en la cabecera `X-API-Version: v1` (también en 4xx/5xx). `info.version` es la del paquete (`version()`; si no está instalado, `0.0.0`). OpenAPI no fija `servers` a 127.0.0.1:8000. El 422/`HTTPValidationError` de FastAPI se sustituye por un 400 `ErrorResponse`.

### Rutas de demostración

`/api/v1/examples` no es un recurso de negocio: sirve para ejercitar paginación, validación, PUT y 404. **No se monta en `production`** salvo `ENABLE_EXAMPLE_ROUTES=true`. Las issues de cursos, usuarios, etc. reutilizarán los mismos esquemas.

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
  main.py            # Factoría FastAPI perezosa: get_app(), CORS, api_router
  db.py              # Motor async SQLAlchemy 2 y fábrica de sesiones
  middleware.py      # ASGI: versión, 500, tope de cuerpo, preflight rechazado
  errors.py          # Sobre de error y handlers (validación, HTTP)
  exceptions.py      # AccessDenied, NotFound, Validation, PayloadTooLarge
  config/            # Ajustes por variables de entorno (pydantic-settings)
  routers/           # Rutas HTTP (health, health/db y examples)
  schemas/           # Modelos Pydantic (camelCase, errores, paginación)
  services/          # Lógica de aplicación (salud + lista fija de examples)
  models/            # Base SQLAlchemy y mixin PersistentModel
alembic/             # Migraciones (env.py async + versions/)
docker/postgres/     # init-test-db.sql (base estudiomusica_test)
tests/               # Tests de pytest (convenciones + BD aislada)
docker-compose.yml   # Postgres 17 para desarrollo y tests
alembic.ini          # Configuración de Alembic
.env.example         # Plantilla de variables de entorno
pyproject.toml       # Dependencias, ruff, pytest y mypy
uv.lock              # Lockfile reproducible de uv
.python-version      # Python 3.15 (desarrollo)
```

## Versiones fijadas

| Pieza | Versión |
| --- | --- |
| Python | >=3.14 (desarrollo en 3.15) |
| uv | >=0.13.5 |
| FastAPI | 0.162.0 |
| uvicorn | 0.54.0 |
| pydantic-settings | 2.16.1 |
| SQLAlchemy | 2.1.6 (extra asyncio) |
| asyncpg | 0.32.0 |
| Alembic | 1.20.1 |
| pytest-asyncio | 1.4.0 |
| pytest | 9.1.1 |
| ruff | 0.17.2 |
| mypy | 2.4.0 |

Las transitivas quedan ancladas en `uv.lock`. Se eligió [uv](https://docs.astral.sh/uv/) porque instala Python, resuelve dependencias de forma reproducible y es el gestor que recomienda el ecosistema actual de FastAPI / Astral. El extra `fastapi[standard]` no se usa en runtime: arrastra `fastapi-cloud-cli`, Sentry y OpenTelemetry. En producción van `fastapi` + `uvicorn[standard]`; `fastapi-cli` y `httpx2` (el cliente que pide Starlette 1.x para `TestClient`) quedan en el grupo de desarrollo.
