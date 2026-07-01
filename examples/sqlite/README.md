# SQLite driver example

Runnable demo of Ferrum against **SQLite** via the `ferrum-orm[sqlite]` extra (`aiosqlite`).
No Docker or external database process is required.

## Prerequisites

```bash
# From the repo root
uv sync --extra dev
mise run dev
# SQLite driver (included in ferrum-orm[all]; dev extra uses PostgreSQL only)
uv add 'ferrum-orm[sqlite]'   # or: uv sync --extra dev --extra sqlite
```

## Run

```bash
cd examples/sqlite
uv run python main.py
```

The script creates `ferrum_example.db` in this directory, applies the migration plan
in `plans/`, then runs a short CRUD loop.

## What this shows

- DSN scheme `sqlite:///<path>` (file) or `sqlite:///:memory:` (ephemeral)
- Dialect-aware migration apply (`CREATE TABLE IF NOT EXISTS` on SQLite)
- `INTEGER PRIMARY KEY` auto-increment semantics on SQLite
- Explicit `connect(dsn)` — no environment variable required

## Optional: env-based DSN

```bash
export FERRUM_DATABASE_URL="sqlite:///$(pwd)/ferrum_example.db"
uv run python -c "import asyncio; from examples.sqlite.main import main; asyncio.run(main())"
```

For production services, prefer PostgreSQL; SQLite is useful for local tools, tests,
and CI smoke checks.
