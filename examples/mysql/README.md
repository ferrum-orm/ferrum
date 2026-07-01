# MySQL driver example

Demonstrates Ferrum with the **`mysql://`** DSN scheme and the `ferrum-orm[mysql]`
extra (`asyncmy`).

## Prerequisites

```bash
# From the repo root
uv sync --extra dev
mise run dev
uv add 'ferrum-orm[mysql]'
```

## Run

```bash
cd examples/mysql
cp .env.example .env
docker compose up -d
export FERRUM_DATABASE_URL=mysql://ferrum:changeme@127.0.0.1:3306/ferrum_dev
uv run python main.py
```

## What this shows

- DSN scheme `mysql://user:pass@host:3306/database` (also accepts `mysql+asyncmy://`)
- Dialect-specific DDL (`ENGINE=InnoDB`, `CREATE TABLE IF NOT EXISTS`)
- MySQL-specific insert/returning path (no PostgreSQL `RETURNING`; driver adapter handles it)

## Thin-parity caveat

MySQL migration plans from hand-written JSON may emit `INTEGER PRIMARY KEY` without
`AUTO_INCREMENT`. This example passes explicit `id` values on `create()`. Prefer
`ferrum makemigrations` from model metadata for real projects.

## SQL Server (MSSQL)

Ferrum also ships `ferrum-orm[mssql]` (`aioodbc`, `mssql://` / `sqlserver://` DSNs).
A full runnable MSSQL example is not included here because it requires the system
`msodbcsql18` ODBC driver. See `docs/getting-started.md` and unit tests in
`tests/python/unit/test_mssql.py` for DSN shape and dialect behavior.
