# Ferrum + FastAPI Quickstart

A minimal runnable example demonstrating Ferrum with a FastAPI application.

See also:

- [../simple/](../simple/) — async CRUD without a web framework
- [../migrations/](../migrations/) — CLI and migration workflow

## Prerequisites

```bash
# From the repo root
uv sync --extra dev
mise run dev
pip install "ferrum[fastapi]" fastapi uvicorn
```

## Run

```bash
cd examples/fastapi_quickstart
cp .env.example .env   # if present; otherwise set DATABASE_URL
docker compose up -d   # use ../migrations/docker-compose.yml or ferrum init
export DATABASE_URL=postgresql://ferrum:changeme@127.0.0.1:5432/ferrum_dev
export FERRUM_DATABASE_URL="$DATABASE_URL"
uvicorn app:app --reload
```

Apply migrations before hitting routes — see [../migrations/README.md](../migrations/README.md).

## What this example shows

- Model definition with Pydantic v2
- `ferrum_lifespan` for connection pool lifecycle (stores pool on `app.state.ferrum_conn`)
- `get_ferrum_conn` FastAPI dependency — every QuerySet terminal requires `conn`
- Async query patterns (`filter`, `get`, `all`)
