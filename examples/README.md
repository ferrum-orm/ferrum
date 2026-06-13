# Ferrum examples

Runnable samples for local development. Each example assumes PostgreSQL on
`127.0.0.1:5432` and the native extension built (`maturin develop` from the repo
root, or `uv sync --extra dev && mise run dev`).

| Example                                    | What it shows                                             |
| ------------------------------------------ | --------------------------------------------------------- |
| [simple/](simple/)                         | Async CRUD without a web framework                        |
| [migrations/](migrations/)                 | CLI workflow, plan generation, apply, and forward fix-ups |
| [fastapi_quickstart/](fastapi_quickstart/) | Ferrum with FastAPI lifespan helpers                      |

## Quick start (any example)

```bash
# From the repo root
uv sync --extra dev
mise run dev          # builds ferrum._native into the venv

cd examples/simple   # or examples/migrations
cp .env.example .env
docker compose up -d
export FERRUM_DATABASE_URL=postgresql://ferrum:changeme@127.0.0.1:5432/ferrum_dev
```

## CLI overview

```bash
ferrum init                          # scaffold .env.example, docker-compose, .gitignore
ferrum migrations apply PLAN.json --dry-run
ferrum migrations apply PLAN.json --confirm
ferrum migrations apply PLAN.json --confirm --token "$FERRUM_MIGRATION_TOKEN"
```

See [migrations/README.md](migrations/README.md) for the full migration lifecycle,
including how v0.1 handles rollback (forward fix-ups, not `migrate down`).
