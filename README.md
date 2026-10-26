# estudiomusica-backend

Backend FastAPI de EstudioMusica. Sustituye los almacenes JSON del frontend Next.js.

Este repositorio es el esqueleto FastAPI, las convenciones transversales de la API (errores, paginación, OpenAPI y CORS), la persistencia (Postgres, SQLAlchemy 2 async y Alembic), la API de autenticación (`/api/v1/auth`), la matriz de roles/permisos (`/api/v1/admin/system-roles`, `/api/v1/courses/{courseId}/me/permissions`) y el CRUD de categorías de curso (`/api/v1/categories`, `/api/v1/admin/categories`).

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

Con la BD disponible responde HTTP 200 y `{"db":"ok"}`. Si Postgres no responde o la sonda supera `DB_HEALTH_TIMEOUT` (2 s por defecto), HTTP 503 y `{"db":"down"}`, sin credenciales ni el sobre `{error, code, fields}` de las rutas de negocio. La sonda abre **una** conexión asyncpg propia (sin el pool de la app ni `pre_ping`; el `SELECT 1` es el ping), con los mismos parámetros de DSN que el motor (`create_connect_args` del dialecto) más `timeout` / `command_timeout` / `application_name`. Las peticiones concurrentes esperan esa sonda (`asyncio.shield`) con su propio `DB_HEALTH_TIMEOUT`. El resultado se cachea `DB_HEALTH_CACHE_TTL` segundos (1 s por defecto; `0` lo desactiva). Un connect/SELECT/close congelado se abandona con `Connection.terminate`. `GET /health/db` en la raíz es el alias de sonda; no aparece en OpenAPI.

Documentación interactiva (OpenAPI / Swagger UI): [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs).

## Autenticación

`POST /api/v1/auth/signup` `{name,email,password}` → 201 `{user}` y cookie HttpOnly `session`.
`POST /api/v1/auth/login` `{email,password}` → 200 `{user}` y cookie.
`POST /api/v1/auth/logout` → 204 (revoca la sesión y borra la cookie).
`GET /api/v1/auth/me` → 200 `{user, systemRole, isAdmin}` (cookie o `Authorization: Bearer`).

Errores de negocio (slugs en snake_case, convención #91): `email_taken` (409), `invalid_credentials` (401), `account_suspended` (403). Sin sesión o token caducado/manipulado: `unauthorized` (401).

El secreto `AUTH_SECRET` firma un JWT HS256. El valor por defecto solo vale en `development`. La cookie es HttpOnly, SameSite=Lax y Secure fuera de development. Logout escribe `revoked_at` en `auth_sessions` para que el JWT no se pueda reutilizar.

```bash
curl -c /tmp/em-cookies -sS -X POST http://127.0.0.1:8000/api/v1/auth/signup \
  -H 'Content-Type: application/json' \
  -d '{"name":"Ada Lovelace","email":"ada@example.com","password":"password12"}'
curl -b /tmp/em-cookies -sS http://127.0.0.1:8000/api/v1/auth/me
curl -b /tmp/em-cookies -c /tmp/em-cookies -sS -X POST http://127.0.0.1:8000/api/v1/auth/logout
```

## Roles de sistema y permisos de curso

La autorización se comprueba en el servidor y se deniega por defecto. El JWT no lleva permisos: cada petición reevalúa el rol de sistema y la membresía de curso.

Roles de sistema (id en snake_case): `alumno`, `profesor`, `profesor_adjunto`, `admin`. Roles de curso: `student`, `assistant`, `teacher`. Permisos (slugs del frontend): `course:view`, `course:grade`, `course:teach`, `site:manage-admins`, `site:manage-courses`. El admin tiene todos los de curso más los de sitio aunque no esté matriculado.

```text
GET    /api/v1/admin/system-roles
PATCH  /api/v1/admin/system-roles/{id}   {name?, active?, sortOrder?}
DELETE /api/v1/admin/system-roles/{id}   409 si isSystem o está en uso
GET    /api/v1/courses/{courseId}/me/permissions
```

`GET /admin/system-roles` exige `site:manage-admins` (solo admin). Un no-admin recibe `403 {error: forbidden}`. Sin sesión o token inválido: `401 {error: unauthorized}`. Desactivar un rol con usuarios es válido: quienes ya lo tienen lo conservan (aunque quede inactivo). Asignar un rol inactivo responde `409 {error: role_not_assignable}`. `assign_system_role` bloquea el destino `FOR SHARE` (y relee `active`); el PATCH bloquea `FOR UPDATE`, para no clavar a alguien en un rol que se está desactivando. Desactivar `admin` o DELETE de un rol sembrado responde `409 {error: system_role_protected}`.

Dependencias reutilizables: `require_site_permission(...)` y `require_course_permission(...)` (leen `course_id` del path). La asignación de matrícula queda fuera de este ticket (#403). El TODO(#306) de visibilidad multimedia/SM3 del profesor adjunto es el flag `ASSISTANT_MEDIA_AND_SM3_VISIBLE` (por defecto `false`); no se ha inventado un permiso nuevo.

`0003_system_roles` (en `app/migrations/versions/`) crea `system_roles` y `course_memberships`, siembra los cuatro roles y añade `users.system_role_id`. Tras el backfill de 0002, `is_admin` se realinea desde `system_role_id` (fuente de verdad). El CHECK exige `is_admin` no nulo y coherente con el rol. `/auth/me` deriva `isAdmin` del rol. Monta el trigger con `op_create_updated_at_trigger`. Las bases de **dev/staging** creadas con una 0003 anterior (sin este CHECK) hay que **recrearlas**: el id de revisión no cambia, y un `downgrade` + `upgrade` pierde el rol y realinea desde `is_admin`. No hay nada desplegado.

## Categorías de curso

El catálogo tiene **un solo nivel** (regla del front, `category-service.ts`): el padre de una categoría tiene que ser una raíz, y una categoría con hijas se queda en la raíz. Un nieto, un padre que no es raíz o mover un nodo con hijas responde `400 {error: validation, fields: ["parentId"]}`. Cualquier usuario autenticado lee el listado; crear, actualizar y borrar exige `site:manage-courses` (solo admin). Alumno, profesor y profesor adjunto reciben `403 {error: forbidden}` en las mutaciones. Sin sesión o token inválido: `401 {error: unauthorized}`.

```text
GET    /api/v1/categories
POST   /api/v1/admin/categories              {name, parentId?, description?, sortOrder?}
PUT    /api/v1/admin/categories/{id}         {name?, parentId?, description?, sortOrder?}
DELETE /api/v1/admin/categories/{id}         409 si tiene hijas
```

El ticket #400 pedía PATCH. Se mantiene **PUT** (convención #91) pero **parcial**: un campo omitido no cambia. `PUT {name}` no mueve a la raíz ni borra `description`. Para quitar el padre o la description hay que enviar `null` explícito (`model_fields_set`). El listado no pagina. Nombre: NFC, espacios colapsados, citext (sin distinguir mayúsculas), único entre hermanos vigentes por `name_key` (columna CITEXT normal, calculada en Python: sin Cf de relleno ni Cn; ZWNJ CONTEXTJ RFC 5892 A.1; ZWJ CONTEXTJ RFC 5892 A.2 tras virama; ZWJ entre emoji según UTS #51, no IDNA; tags solo en banderas RGI gbeng/gbsct/gbwls). DELETE es lógico (`deleted_at`). `409 {error: category_not_empty}` si hay hijas vigentes. Hasta #97 no hay tabla `courses`: el borrado no protege cursos. `409 {error: category_name_taken}` si el nombre ya existe en el mismo padre. `--fix` toma `LOCK TABLE categories IN EXCLUSIVE MODE` antes del SELECT (sin advisory global en POST/PUT/DELETE) y renombra duplicados con el sufijo ` (dup N)` en una sola transacción. Al validar el padre se hace `SELECT … FOR SHARE`. `uv run python scripts/verify_category_name_keys.py` recalcula las claves y avisa diferencias o colisiones. Un deadlock (`40P01`) o un fallo de serialización (`40001`) es transitorio: el servidor reintenta la unidad de trabajo hasta 2 veces (backoff corto con jitter). Si sigue fallando, o si el conflicto aparece en un GET o en el COMMIT de la petición, responde **503** `{error: transient_db_conflict}` con `Retry-After: 1`; el cliente puede reintentar. Cada deadlock espera `deadlock_timeout` (~1 s por defecto), así que un 503 agotado tarda ~3 s. No se responde 500 por `40P01`/`40001`.

Las filas ya guardadas cuyo `name` o `description` hoy se rechazarían (RLO, U+FFFC, tag suelto, bidi, Cn) siguen funcionando con GET, `PUT {sortOrder}` y DELETE. Reenviar el mismo `name` o `description` en un PUT da 400: no hacer "echo" del GET. Un PUT que solo toca `sortOrder` o `description` recalcula `name_key` y puede dar 409 si esa clave choca con una hermana vigente.

`0004_course_categories` crea la tabla `categories` (`id`, `name`, `name_key` CITEXT persistido por la app, `parent_id`, `description`, `sort_order`, timestamps, `deleted_at`) y el trigger `updated_at`. No hay función SQL de clave. #97 añadirá `courses.category_id` FK a esta tabla.

La 0004 cambió durante la revisión (78b08eaf, 8924e0a0, 510acf77) y no ha estado en `main` ni en BD fuera de desarrollo/revisión. Una BD de desarrollo migrada con esas versiones hay que recrearla o hacer `alembic downgrade 0003_system_roles && alembic upgrade head` (**borra las categorías**). Alternativa sin pérdida (8924e0a0 y 510acf77): `ALTER TABLE categories ALTER name_key DROP EXPRESSION; DROP FUNCTION public.category_name_key(public.citext);` y backfill de `name_key` en Python, deduplicando con `uv run python scripts/verify_category_name_keys.py --fix`. Ese `ALTER`/`DROP FUNCTION` **falla en 78b08eaf** (no hay columna ni función). Camino sin pérdida para 78b08eaf:

```sql
CREATE TABLE categories_bak AS TABLE categories;
```

```bash
alembic downgrade 0003_system_roles
alembic upgrade head
```

```sql
INSERT INTO categories (
    id, created_at, updated_at, deleted_at,
    name, name_key, parent_id, description, sort_order
)
SELECT
    id, created_at, updated_at, deleted_at,
    name, id::text, parent_id, description, sort_order
FROM categories_bak;
```

```bash
uv run python scripts/verify_category_name_keys.py --fix
```

```sql
DROP TABLE categories_bak;
```

## Postgres, migraciones y tests con BD

El DSN se lee de `DATABASE_URL` (pydantic-settings). El valor por defecto coincide con `docker-compose.yml`:

```text
postgresql+asyncpg://estudiomusica:estudiomusica@127.0.0.1:5432/estudiomusica
```

### Arrancar Postgres en local

```bash
docker compose up -d
```

Eso levanta Postgres 17 solo en `127.0.0.1:5432` y crea `estudiomusica` (desarrollo), `estudiomusica_test` y `estudiomusica_alembic_test`. Fuera de `development` Settings exige usuario, contraseña y base **explícitos** en el DSN (no se heredan de `PGUSER` / `PGPASSWORD` / `PGDATABASE`; asyncpg usaría el usuario como nombre de base) y rechaza la contraseña por defecto, el usuario por defecto (`estudiomusica`, p. ej. `estudiomusica:otra@…`, también en la query) y las bases `*_test` (path o query, sin distinguir mayúsculas, p. ej. `x_TEST`). También rechaza autenticación peer, `.pgpass` (`passfile`) y certificado de cliente (`sslcert` / `sslkey` / `sslpassword` / `sslrootcert`). Los overrides de query (`password`, `user`, `database`, `dbname`) se aplican antes de validar. `sslmode=` de la query se traduce a `ssl=` (asyncpg no acepta `sslmode` como kwarg). Los tests de production activan un opt-in (`allow_test_database`) que **no** lee el entorno. En development un DSN sin credenciales sí puede heredar `PGUSER`/`PGPASSWORD`. Los errores de validación no muestran el DSN ni `AUTH_SECRET` (`hide_input_in_errors`; tampoco en `.errors()` / `.json()`). Espera a que el healthcheck esté sano:

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

Los modelos de negocio heredan `PersistentModel` (`app/models/base.py`): `id` UUID, `created_at`, `updated_at` y borrado lógico `deleted_at`. `updated_at` va con `clock_timestamp()` y un trigger `BEFORE UPDATE` montado por las migraciones (`op_create_updated_at_trigger` / hook de autogenerate); `after_create` solo cubre `create_all` en tests. No hay índice genérico en `deleted_at` (añadir uno parcial `WHERE deleted_at IS NULL` en cada tabla que liste vigentes). `0002_auth_users` crea `citext` si falta, `users` y `auth_sessions`, y monta ese trigger en ambas. El downgrade de 0002 **no** hace `DROP EXTENSION citext` (puede venir preinstalada o usarla otra revisión). `0003_system_roles` añade `system_roles` (id slug, no UUID), `users.system_role_id` y `course_memberships`. `0004_course_categories` añade `categories` (árbol con `parent_id`, citext, `name_key` persistido por la app, índices únicos parciales por hermano) y monta el trigger con `op_create_updated_at_trigger` (0001 solo crea la función; el SQL va quoted por el preparer). La 0004 cambió en revisión (78b08eaf, 8924e0a0, 510acf77): recrear o `downgrade 0003_system_roles && upgrade head` (borra categorías); alternativa sin pérdida arriba (el `DROP` literal no aplica a 78b08eaf).

`env.py` usa `sqlalchemy.url` de Alembic si está definido; si no, `DATABASE_URL`.

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

El fixture `production_client` crea el rol `em_prod_tests` (LOGIN, **sin** SUPERUSER / CREATEDB / CREATEROLE) con contraseña aleatoria por ejecución y lo borra al terminar la suite, también si falla. Grants mínimos, los mismos que un rol de producción:

- `CONNECT` en la base de test
- `USAGE` en el esquema `public`
- `SELECT, INSERT, UPDATE, DELETE` en las tablas de `public` (`users`, `auth_sessions`, `system_roles`, `course_memberships`, `categories`)
- `USAGE, SELECT` en las secuencias de `public`

No puede migrar ni crear bases. Las filas de esos tests (`prod-%@example.com`) se borran al terminar.

## Tests, lint y types

Desde la raíz del repositorio:

```bash
uv run pytest
uv run pytest -W error
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

Los tests de production usan el fixture `production_client` (`APP_ENV=production` contra la base `*_test` con rol `em_prod_tests`). `APP_ENV=prod` no es un valor válido y no ejercita ese modo.

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
  routers/           # Rutas HTTP (health, auth, admin, categories, courses y examples)
  schemas/           # Modelos Pydantic (camelCase, errores, paginación, auth, roles, categories)
  services/          # Lógica de aplicación (salud, auth, roles, categories, examples)
  permissions/       # Matriz versionada y dependencias require_*_permission
  auth/              # Hash argon2, JWT HS256 y cookie de sesión
  models/            # PersistentModel, User, AuthSession, SystemRoleRow, CourseMembership, Category
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
| argon2-cffi | 25.1.0 |
| PyJWT | 2.15.1 |
| email-validator | 2.3.0 |
| pytest-asyncio | 1.4.0 |
| pytest | 9.1.1 |
| pytest-timeout | 2.4.0 |
| ruff | 0.17.2 |
| mypy | 2.4.0 |

Las transitivas quedan ancladas en `uv.lock`. Se eligió [uv](https://docs.astral.sh/uv/) porque instala Python, resuelve dependencias de forma reproducible y es el gestor que recomienda el ecosistema actual de FastAPI / Astral. El extra `fastapi[standard]` no se usa en runtime: arrastra `fastapi-cloud-cli`, Sentry y OpenTelemetry. En producción van `fastapi` + `uvicorn[standard]`; `fastapi-cli` y `httpx2` (el cliente que pide Starlette 1.x para `TestClient`) quedan en el grupo de desarrollo.
