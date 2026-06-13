# Simple async example (no web framework)

Minimal script showing Ferrum models, connection pooling, and QuerySet terminals
without FastAPI or Starlette.

## Prerequisites

```bash
# From the repo root
uv sync --extra dev
mise run dev
```

## Run

```bash
cd examples/simple
cp .env.example .env
docker compose up -d
export FERRUM_DATABASE_URL=postgresql://ferrum:changeme@127.0.0.1:5432/ferrum_dev

# Create tables first (see ../migrations/)
uv run python ../migrations/generate_plan.py --write plans/001_create_note.json
ferrum migrations apply plans/001_create_note.json --confirm

# Run the demo
uv run python main.py
```

## What `main.py` demonstrates

- Defining a Pydantic v2 `Model` subclass
- Opening a pool with `async with ferrum.connect() as conn`
- `create`, `filter`, `get`, `update`, `delete`, and `count` — all require `conn`
- Tier A observability hooks via `ferrum.register_hook`

Every terminal method is `async` and takes an explicit `Connection`. There is no
sync compatibility layer.
