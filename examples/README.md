# Ferrum examples

Runnable samples for local development. Each example assumes the native extension
is built (`maturin develop` from the repo root, or `uv sync --extra dev && mise run dev`).

## Examples

| Example                                    | Driver / config                          | What it shows                                         |
| ------------------------------------------ | ---------------------------------------- | ----------------------------------------------------- |
| [simple/](simple/)                         | PostgreSQL, `FERRUM_DATABASE_URL`        | Async CRUD without a web framework                    |
| [migrations/](migrations/)                 | PostgreSQL                               | CLI workflow, plan generation, apply, forward fix-ups |
| [fastapi_quickstart/](fastapi_quickstart/) | PostgreSQL, `DATABASE_URL`               | Ferrum with FastAPI lifespan helpers                  |
| [sqlite/](sqlite/)                         | SQLite (`sqlite:///`)                    | Zero-Docker local DB; explicit DSN                    |
| [mysql/](mysql/)                           | MySQL (`mysql://`)                       | `asyncmy` driver + Docker Compose                     |
| [pyproject_config/](pyproject_config/)     | PostgreSQL, `DATABASE_URL` via pyproject | `[ferrum]` in `pyproject.toml` (no `ferrum.toml`)     |

### Driver extras

| Backend    | Install extra        | DSN scheme examples                        |
| ---------- | -------------------- | ------------------------------------------ |
| PostgreSQL | `ferrum-orm[pg]`     | `postgresql://`, `postgres://`             |
| MySQL      | `ferrum-orm[mysql]`  | `mysql://`, `mysql+asyncmy://`             |
| SQLite     | `ferrum-orm[sqlite]` | `sqlite:///path.db`, `sqlite:///:memory:`  |
| SQL Server | `ferrum-orm[mssql]`  | `mssql://`, `sqlserver://` (requires ODBC) |

## Quick start (PostgreSQL examples)

```bash
# From the repo root
uv sync --extra dev
mise run dev          # builds ferrum._native into the venv

cd examples/simple   # or examples/migrations, examples/pyproject_config
cp .env.example .env
docker compose up -d
export FERRUM_DATABASE_URL=postgresql://ferrum:changeme@127.0.0.1:5432/ferrum_dev
```

For `pyproject_config`, use `DATABASE_URL` instead — see that example's README.

## CLI overview

```bash
ferrum init                          # scaffold .env.example, docker-compose, .gitignore
ferrum migrations apply PLAN.json --dry-run
ferrum migrations apply PLAN.json --confirm
ferrum migrations apply PLAN.json --confirm --token "$FERRUM_MIGRATION_TOKEN"
```

See [migrations/README.md](migrations/README.md) for the full migration lifecycle,
including how v0.1 handles rollback (forward fix-ups, not `migrate down`).
