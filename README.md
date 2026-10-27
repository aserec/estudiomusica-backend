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

Con la BD disponible responde HTTP 200 y `{"db":"ok"}`. Si Postgres no responde o la sonda supera `DB_HEALTH_TIMEOUT` (2 s por defecto), HTTP 503 y `{"db":"down"}`, sin credenciales ni el sobre `{error, code, fields}` de las rutas de negocio. La sonda abre **una** conexión asyncpg propia (sin el pool de la app ni `pre_ping`; el `SELECT 1` es el ping), con los parámetros del DSN del dialecto (`create_connect_args`, sin las claves del adaptador `prepared_statement_cache_size` / `prepared_statement_name_func` / `async_creator_fn`) más `timeout` / `command_timeout` / `application_name`. Una clave extra (`?foo=bar`) tumba la sonda igual que la app. Las peticiones concurrentes esperan esa sonda (`asyncio.shield`) con su propio `DB_HEALTH_TIMEOUT`. El resultado se cachea `DB_HEALTH_CACHE_TTL` segundos (1 s por defecto, tope 60 s; `0` lo desactiva). Un connect/SELECT/close congelado se abandona con `Connection.terminate`. `GET /health/db` en la raíz es el alias de sonda; no aparece en OpenAPI.

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

Eso levanta Postgres 17 solo en `127.0.0.1:5432` y crea `estudiomusica` (desarrollo), `estudiomusica_test` y `estudiomusica_alembic_test`. Fuera de `development` Settings exige usuario, contraseña y base **explícitos** en el DSN (no se heredan de `PGUSER` / `PGPASSWORD` / `PGDATABASE`; asyncpg usaría el usuario como nombre de base) y rechaza la contraseña por defecto, el usuario por defecto (`estudiomusica`, p. ej. `estudiomusica:otra@…`) y las bases `*_test` (sin distinguir mayúsculas, p. ej. `x_TEST`). También rechaza `.pgpass` (`passfile` en la query o `PGPASSFILE`) y certificado de cliente (`sslcert` / `sslkey` / `sslpassword` en la query, o `PGSSLCERT` / `PGSSLKEY`). `sslrootcert` en la query se traduce a un `SSLContext` con `cafile` (asyncpg no tiene ese kwarg); `PGSSLROOTCERT` es la alternativa por entorno. Ambos se permiten (`verify-full`) y aceptan una CA sin `keyUsage`, como libpq (Python 3.13+ activa `VERIFY_X509_STRICT` en `create_default_context`). Un `sslrootcert` que solo contenga una intermedia no basta: no se activa `VERIFY_X509_PARTIAL_CHAIN` (igual que libpq/asyncpg). `ssl=require` + `sslrootcert` verifica CA y hostname a propósito: es más estricto que libpq (`require` no comprueba ni CA ni hostname; libpq trata `require`+`root.crt` como `verify-ca`). `ssl=allow` + `sslrootcert` también verifica la CA (sin hostname): más estricto que libpq, que en `allow` no exige certificado. El cliente no puede rechazar autenticación peer: eso lo decide `pg_hba.conf`. No se validan `PGSSLMODE`, `PGSSLPASSWORD`, `?service=` / `PGSERVICEFILE` ni los certificados por defecto de `~/.postgresql`. Los overrides de query (`password`, `user`, `database`, `dbname`) se aplican antes de validar. `sslmode=` de la query se traduce a `ssl=` (asyncpg no acepta `sslmode` como kwarg). `ssl` y `sslmode` juntos o repetidos (también `SSL` / `SSLMODE`) y cualquier clave TLS repetida (`sslrootcert`, `sslcert`, `sslkey`, `sslpassword`) levantan `ValueError`; libpq acepta la repetición y se queda con la última clave. Un valor de `ssl` / `sslmode` desconocido (`ssl=bogus`) también levanta `ValueError` (no acaba en `prefer`). Una clave TLS vacía (`ssl=`, `sslmode=`) o una query percent-encoded entera (`sslmode%3Dverify-full%26ssl%3Ddisable`) también levantan `ValueError` (libpq las rechaza; sin esto quedarían como `prefer`). En development un DSN sin credenciales sí puede heredar `PGUSER`/`PGPASSWORD`. Los errores de validación no muestran el DSN (`hide_input_in_errors`; tampoco en `.errors()` / `.json()`). Espera a que el healthcheck esté sano:

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

Las revisiones viven en `app/migrations/versions/` (`script_location = app:migrations`). Van en el wheel con el paquete `app`. La fuente de verdad es `app/alembic.ini`. El `alembic.ini` de la raíz es un fichero regular (no un symlink) con las mismas claves operativas para que `uv run alembic` funcione en el repo y el sdist no lleve enlaces.

Desde el paquete instalado, en un directorio **sin** un `app/` local (`python -c` y `python -m alembic` importan el `app/` del CWD):

```bash
python -c "from alembic import command; from app.migrations.config import make_alembic_config; command.upgrade(make_alembic_config(), 'head')"
```

En el repo, o con el wheel, también vale `alembic -c app/alembic.ini upgrade head` (o `-c` apuntando al `alembic.ini` de `site-packages/app`).

`0001` solo crea la función `set_updated_at()` (`clock_timestamp()`), no el trigger. No instala `pgcrypto`: en Postgres >= 13 `gen_random_uuid()` es nativa. El trigger de cada tabla `PersistentModel` lo montan las revisiones que la crean (`op_create_updated_at_trigger` / hook de autogenerate); `after_create` solo cubre `create_all` en tests. Nueva revisión, cuando exista un modelo:

```bash
uv run alembic revision -m "descripcion" --autogenerate
```

Los modelos de negocio heredan `PersistentModel` (`app/models/base.py`): `id` UUID, `created_at`, `updated_at` y borrado lógico `deleted_at`. `updated_at` va con `clock_timestamp()` y un trigger `BEFORE UPDATE` montado por las migraciones; no hay índice genérico en `deleted_at` (añadir uno parcial `WHERE deleted_at IS NULL` en cada tabla que liste vigentes).

`env.py` usa `sqlalchemy.url` de Alembic si está definido; si no, `DATABASE_URL`.

Las rutas inyectan la sesión con **`SessionDep`** (`Depends` + `scope="function"`). `get_session` ya no es pública: el scope por defecto de FastAPI 0.162 (`request`) hacía commit *después* de responder (201 con la fila aún no visible). Las ramas #93–#97 deben importar `SessionDep`. Un `StreamingResponse` que consulte la sesión usa **`SessionStreamingDep`** (scope `request`).

### Tests con base aislada

Los tests de persistencia **no** usan los JSON de `.data/` del frontend. Usan `estudiomusica_test` y envuelven cada caso en una transacción que se revierte al terminar. `ALEMBIC_TEST_DATABASE_URL` se deriva de `TEST_DATABASE_URL` (`<stem>_alembic_test`, p. ej. `estudiomusica_alembic_test`) y solo se usa en upgrade/downgrade de Alembic. El nombre efectivo de ambas bases debe terminar en `_test`; se rechazan query params que puedan cambiar el destino (`database`, `dbname`, `service`, `options`). Si no, pytest aborta (el mensaje nunca incluye la contraseña).

`docker/postgres/init-test-db.sql` solo corre la primera vez que se crea el volumen `postgres_data`. Si el volumen ya existía, `pytest` crea las bases `*_test` conectando a la BD de mantenimiento `postgres`. Para recrearlas a mano: `docker compose down -v && docker compose up -d`.

```bash
docker compose up -d
uv run pytest
```

Si Postgres no está en `127.0.0.1:5432`, apunta `TEST_DATABASE_URL` a tu DSN async:

```bash
export TEST_DATABASE_URL=postgresql+asyncpg://estudiomusica:estudiomusica@127.0.0.1:5432/estudiomusica_test
uv run pytest
```

`uv run pytest` sin Postgres en marcha **salta** los tests de BD solo si el error es de conexión (refused, host desconocido, timeout). Un fallo de autenticación o una migración rota **falla**. En CI: `REQUIRE_POSTGRES=1 uv run pytest` (también `true`/`yes`/`on`, como Pydantic) convierte el skip en fallo; un valor desconocido aborta. Los mensajes y `--showlocals` no incluyen la contraseña. Los tests de convenciones (errores, paginación, CORS, `/health`) no necesitan la base.

Los tests de `?sslrootcert=` (`tls_postgres`) levantan un Postgres TLS efímero con Docker. Si `docker` no está en el `PATH`, se saltan, salvo `REQUIRE_TLS_TESTS=1` (entonces fallan). Para reutilizar un servidor TLS ya levantado:

```bash
export TLS_TEST_DATABASE_URL=postgresql+asyncpg://tlsprobe:tlsprobe-secret@127.0.0.1:55433/tlsprobe_test
export TLS_TEST_CAFILE=/ruta/al/ca.pem
uv run pytest
```

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
  migrations/        # Alembic (env.py async + versions/ + config programático)
  alembic.ini        # Fuente de verdad empaquetada
docker/postgres/     # init-test-db.sql (bases *_test; solo al crear el volumen)
tests/               # Tests de pytest (convenciones + BD aislada)
docker-compose.yml   # Postgres 17 en 127.0.0.1:5432 (sin container_name fijo)
alembic.ini          # CLI del repo (copia regular; no es un symlink)
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
| pytest-timeout | 2.4.0 |
| ruff | 0.17.2 |
| mypy | 2.4.0 |

Las transitivas quedan ancladas en `uv.lock`. Se eligió [uv](https://docs.astral.sh/uv/) porque instala Python, resuelve dependencias de forma reproducible y es el gestor que recomienda el ecosistema actual de FastAPI / Astral. El extra `fastapi[standard]` no se usa en runtime: arrastra `fastapi-cloud-cli`, Sentry y OpenTelemetry. En producción van `fastapi` + `uvicorn[standard]`; `fastapi-cli` y `httpx2` (el cliente que pide Starlette 1.x para `TestClient`) quedan en el grupo de desarrollo.
