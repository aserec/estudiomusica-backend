# estudiomusica-backend

Backend FastAPI de EstudioMusica. Sustituye los almacenes JSON del frontend Next.js.

Este repositorio es todavía el esqueleto inicial: no incluye base de datos ni lógica de negocio. Las siguientes issues añadirán Postgres + SQLAlchemy 2 async + Alembic (incluido `GET /api/v1/health/db`) y el resto de convenciones de API (errores, paginación, OpenAPI y CORS).

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
uv run fastapi dev app/main.py
```

El servidor queda en [http://127.0.0.1:8000](http://127.0.0.1:8000). Equivalente con Uvicorn:

```bash
uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Host y puerto **no** se leen del `.env`: `fastapi dev` y Uvicorn usan 127.0.0.1:8000 por defecto. Cámbialos con las flags del comando:

```bash
uv run fastapi dev app/main.py --host 127.0.0.1 --port 8001
uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8001
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

## Convenciones de API

Decisión del 24 de octubre de 2026 (issue #1):

- Prefijo global `/api/v1`, aplicado por el router. Las rutas de las issues son relativas a él (`GET /health` → `GET /api/v1/health`).
- Claves JSON en camelCase.
- Paginación con `page` / `pageSize`.
- `PUT` para las actualizaciones, salvo el `PATCH` de autoguardado de la issue #62.
- Códigos de error y valores de enum en snake_case (`invalid_score`, `in_progress`, `multichoice_single`). La regla camelCase aplica solo a las claves.

El formato de errores, la paginación y CORS se implementarán en la issue de convenciones. `GET /api/v1/health/db` se añadirá cuando exista la base de datos.

## Estructura de carpetas

```text
app/
  main.py            # Factoría FastAPI y punto de entrada ASGI
  config/            # Ajustes por variables de entorno (pydantic-settings)
  routers/           # Rutas HTTP (GET /health, montada bajo /api/v1)
  schemas/           # Modelos Pydantic de request/response
  services/          # Lógica de aplicación (vacío salvo el chequeo de salud)
  models/            # Reservado para modelos SQLAlchemy
tests/               # Tests de pytest
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
| pytest | 9.1.1 |
| ruff | 0.17.2 |
| mypy | 2.4.0 |

Las transitivas quedan ancladas en `uv.lock`. Se eligió [uv](https://docs.astral.sh/uv/) porque instala Python, resuelve dependencias de forma reproducible y es el gestor que recomienda el ecosistema actual de FastAPI / Astral. El extra `fastapi[standard]` no se usa en runtime: arrastra `fastapi-cloud-cli`, Sentry y OpenTelemetry. En producción van `fastapi` + `uvicorn[standard]`; `fastapi-cli`, `httpx` y `httpx2` (el cliente que pide Starlette 1.10 para `TestClient`) quedan en el grupo de desarrollo.
