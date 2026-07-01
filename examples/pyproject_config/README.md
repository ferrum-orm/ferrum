# Pyproject.toml configuration example

Shows Ferrum project settings **colocated in `pyproject.toml`** instead of a
standalone `ferrum.toml`, and a **custom database URL env var** (`DATABASE_URL`
only — not `FERRUM_DATABASE_URL`).

There is intentionally **no** `ferrum.toml` in this directory.

## Prerequisites

```bash
# From the repo root
uv sync --extra dev
mise run dev
```

## Run

```bash
cd examples/pyproject_config
cp .env.example .env
docker compose up -d

# DATABASE_URL is the only env var this example reads (via pyproject.toml).
export DATABASE_URL=postgresql://ferrum:changeme@127.0.0.1:5432/ferrum_dev

ferrum migrations apply plans/001_create_note.json --confirm
uv run python main.py
```

## What this shows

| Topic           | This example                                                      |
| --------------- | ----------------------------------------------------------------- |
| Config file     | `[ferrum]` table in `pyproject.toml` (no `ferrum.toml`)           |
| Database URL    | `database_url_env = "DATABASE_URL"` — skips `FERRUM_DATABASE_URL` |
| Model discovery | `settings = "ferrum_conf"` imports `models.Note` for CLI          |
| Migrations dir  | `migrations_dir = "plans"`                                        |
| Dotenv          | `env_file = ".env"` loaded by the Ferrum CLI bootstrap            |

## Discovery order reminder

When you run `ferrum` or `connect()` from this directory, Ferrum walks up from
`cwd` and stops at the first directory containing `ferrum.toml` **or**
`pyproject.toml`. Run commands from `examples/pyproject_config/` so this
example's `pyproject.toml` wins over the repo-root package manifest.

## Twelve-factor / platform defaults

Many platforms (Heroku, Railway, Render, Fly.io, Docker Compose templates) export
`DATABASE_URL`. Point Ferrum at that convention without renaming variables:

```toml
[ferrum]
database_url_env = "DATABASE_URL"
```

`FERRUM_DATABASE_URL` remains the default when `database_url_env` is unset.
