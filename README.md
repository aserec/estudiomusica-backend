# estudiomusica-backend

Backend FastAPI de EstudioMusica. Sustituye los almacenes JSON del frontend Next.js.

Este repositorio es todavía el esqueleto inicial: no incluye base de datos ni lógica de negocio. Las siguientes issues añadirán Postgres + SQLAlchemy 2 async + Alembic y las convenciones de API (errores, paginación, OpenAPI y CORS).

## Requisitos

- [Python](https://www.python.org/downloads/) **3.15** (la versión fijada del proyecto)
- [uv](https://docs.astral.sh/uv/) (instalador, entornos virtuales y lockfile)

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
uv sync
```

`uv sync` crea `.venv`, instala las dependencias de runtime y de desarrollo según `uv.lock` (versiones reproducibles) y deja el paquete `app` instalado en modo editable.

## Arrancar en local

```bash
uv run fastapi dev app/main.py
```

El servidor queda en [http://127.0.0.1:8000](http://127.0.0.1:8000). Equivalente con Uvicorn:

```bash
uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Comprueba que responde:

```bash
curl http://127.0.0.1:8000/health
```

Debe devolver HTTP 200 y:

```json
{"status":"ok"}
```

Documentación interactiva (OpenAPI / Swagger UI): [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs).

El host y el puerto se pueden cambiar con `HOST` y `PORT` en `.env`.

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

## Estructura de carpetas

```text
app/
  main.py            # Factoría FastAPI y punto de entrada ASGI
  config/            # Ajustes por variables de entorno (pydantic-settings)
  routers/           # Rutas HTTP (ahora solo GET /health)
  schemas/           # Modelos Pydantic de request/response
  services/          # Lógica de aplicación (vacío salvo el chequeo de salud)
  models/            # Reservado para modelos SQLAlchemy
tests/               # Tests de pytest
.env.example         # Plantilla de variables de entorno
pyproject.toml       # Dependencias, ruff, pytest y mypy
uv.lock              # Lockfile reproducible de uv
.python-version      # Python 3.15
```

El prefijo `/api/v1`, el formato de errores, la paginación y CORS se dejarán para la issue de convenciones de API. `GET /health` permanece en la raíz a propósito: es un liveness check, no un recurso de negocio. `GET /health/db` se añadirá cuando exista la base de datos.

## Versiones fijadas

| Pieza | Versión |
| --- | --- |
| Python | 3.15 |
| FastAPI | 0.162.0 |
| pydantic-settings | 2.16.1 |
| pytest | 9.1.1 |
| ruff | 0.17.2 |
| mypy | 2.4.0 |

Las transitivas quedan ancladas en `uv.lock`. Se eligió [uv](https://docs.astral.sh/uv/) porque instala Python, resuelve dependencias de forma reproducible y es el gestor que recomienda el ecosistema actual de FastAPI / Astral.
