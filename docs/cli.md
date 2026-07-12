# CLI Guide

The Ferrum CLI (`ferrum`) is a Typer app for project scaffolding, migrations, and
schema introspection. It ships behind the optional `cli` extra — library code never
imports it.

---

## 1. Install

```bash
pip install 'ferrum-orm[cli]'
# recommended for local .env loading:
pip install 'ferrum-orm[cli,dotenv]'

# with a database driver:
pip install 'ferrum-orm[cli,dotenv,pg]'      # PostgreSQL (asyncpg)
pip install 'ferrum-orm[cli,dotenv,mysql]'   # MySQL (asyncmy)
pip install 'ferrum-orm[cli,dotenv,sqlite]'  # SQLite (aiosqlite)
pip install 'ferrum-orm[cli,dotenv,mssql]'   # SQL Server (aioodbc)
```

Console script: `ferrum`. Help: `ferrum --help`.

The native extension (`ferrum._native`) must be built for query terminals; migration
planning/SQL emission works from Python, but live `migrate` / `inspectdb` need a
working driver + DSN.

---

## 2. Bootstrap (runs before every subcommand)

On entry the CLI:

1. Finds the project root (`ferrum.toml` or `pyproject.toml` walking up from cwd).
2. Loads dotenv (`[ferrum].env_file`, default `.env`) with **`override=False`**
   (already-set env vars win). Skips silently if `python-dotenv` or the file is missing.
3. Imports the settings / models module so `makemigrations` can see `Model` subclasses.

**Discovery order for settings:**

1. `FERRUM_SETTINGS` env var
2. `[ferrum].settings` in `ferrum.toml` / `pyproject.toml`
3. Autodiscover `ferrum_conf.py` in the project root

If (1) or (2) is set and import fails → `FerrumConfigError` `[FERR-C001]`. Missing
autodiscovery is silent (back-compat).

`ferrum.connect()` in library code stays env-only — **no** dotenv, **no** auto-import.
Bootstrap is CLI-only by design.

### `ferrum_conf.py` (model import hook)

```python
# ferrum_conf.py — import every module that defines Model subclasses
from myapp import models  # noqa: F401
```

Without this (or an equivalent settings module), `makemigrations` / `resetdb` see an
empty model registry.

### Config keys (`[ferrum]`)

| Key                | Default                                   | Role                                       |
| ------------------ | ----------------------------------------- | ------------------------------------------ |
| `settings`         | unset                                     | Module imported at CLI bootstrap           |
| `migrations_dir`   | `migrations`                              | Migration file directory                   |
| `default_env`      | `development`                             | Default `--env` for migrate-style commands |
| `env_file`         | `.env`                                    | Dotenv path relative to project root       |
| `database_url_env` | `FERRUM_DATABASE_URL` then `DATABASE_URL` | Env var name for the DSN                   |

Secrets stay in `.env`, never in TOML.

---

## 3. Commands

### `ferrum init`

Scaffold a project directory:

```bash
ferrum init --name myproject
ferrum init --force   # overwrite scaffold files in cwd
```

Creates (when missing): `ferrum.toml`, `.gitignore` (excludes `.env`), `.env.example`,
`docker-compose.yml` (Postgres 16 bound to **127.0.0.1 only**).

**Safety:** refuses paths outside cwd (no absolute / symlink escape). Does not write
real credentials.

**Driver note:** the scaffolded compose file is **PostgreSQL**. For MySQL / MSSQL /
SQLite, provide your own database and set `FERRUM_DATABASE_URL` accordingly — `init`
does not emit those compose stacks.

---

### `ferrum makemigrations`

Diff registered models against prior migration state and write `NNNN_slug.py` files.
**No database connection.**

```bash
ferrum makemigrations
ferrum makemigrations --name add_user_email
ferrum makemigrations --migrations-dir path/to/migrations
```

Models must be imported via bootstrap (`ferrum_conf` / settings). Internally walks
`Model.__subclasses__()` recursively.

---

### `ferrum migrate`

Apply pending migrations in dependency order.

```bash
ferrum migrate
ferrum migrate --dry-run
ferrum migrate --env production --confirm
ferrum migrate --migrations-dir ./migrations
```

| Flag        | Effect                                                    |
| ----------- | --------------------------------------------------------- |
| `--dry-run` | Print ops; apply nothing                                  |
| `--confirm` | Required for destructive ops and for `env != development` |
| `--env`     | Environment label (default `development`)                 |

Behavior:

- Ensures `ferrum_migrations` ledger exists (DDL is dialect-aware).
- Verifies checksums of already-applied files (`[FERR-M005]` if edited).
- Best-effort schema-drift warning when models are loaded.
- Executes ops with dialect from the **live connection** (`conn.dialect`).

**Failure modes:** missing DSN → `FerrumConfigError`; safety gate →
`FerrumMigrationError`; mid-migration DB errors wrap as `FerrumMigrationError` with
sanitized messages (no passwords / row DETAIL).

#### Per-driver apply notes

| Dialect      | Notes                                                                                                                                                                                                   |
| ------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **postgres** | Full op set: extensions, RLS, functions, `USING` indexes, `VECTOR`, etc. Non-transactional kinds (e.g. some extension paths) follow ADR-004 classification.                                             |
| **mysql**    | Thin parity: btree indexes, tables, FTS `FULLTEXT`; no PG extensions/RLS.                                                                                                                               |
| **sqlite**   | File DSN; FTS5 virtual tables via FTS ops; limited alter surface.                                                                                                                                       |
| **mssql**    | Thin parity: rejects `create_extension`, RLS, `alter_column`, `rename_column`, function DDL. Index create has no `IF NOT EXISTS` (ledger provides once-only). FTS needs catalog + async population lag. |

DSN schemes: `postgresql://`, `mysql://`, `sqlite:///…`, `mssql://` / `sqlserver://`.

---

### `ferrum showmigrations`

```bash
ferrum showmigrations
ferrum showmigrations --migrations-dir ./migrations
```

Lists `[X]` applied, `[!]` checksum mismatch, `[ ]` pending. Connects to the DB for
ledger state.

---

### `ferrum revert`

```bash
ferrum revert --confirm
ferrum revert --target 0001_initial --confirm
ferrum revert --env staging --confirm
```

Runs `reverse_operations` and removes the ledger entry. Migration **files stay on disk**
(Django-style). Destructive reverse ops need `--confirm`. Non-development `--env`
needs `--confirm`.

Dialect behavior matches `migrate` (SQL emitted for the connected driver).

---

### `ferrum sqlmigrate`

Offline SQL for one migration file — **no DB connection**.

```bash
ferrum sqlmigrate 0001_create_note
ferrum sqlmigrate 0002_add_vector --dialect mssql
```

| Flag        | Default    | Values                                 |
| ----------- | ---------- | -------------------------------------- |
| `--dialect` | `postgres` | `postgres`, `mysql`, `sqlite`, `mssql` |

Use this to preview how the same ops render on each backend before applying.

---

### `ferrum inspectdb`

Introspect an existing schema into Ferrum model source.

```bash
ferrum inspectdb
ferrum inspectdb -o models_generated.py
ferrum inspectdb --schema public -o ./generated/
```

| Flag              | Default  | Notes             |
| ----------------- | -------- | ----------------- |
| `-o` / `--output` | stdout   | File or directory |
| `--schema`        | `public` | Schema to scan    |

**PostgreSQL only.** Other dialects print `inspectdb currently supports PostgreSQL only.`
and exit. Introspection uses `information_schema` via `driver.fetch()` (parameterized
schema name). Excludes `ferrum_migrations` and `pg_*` tables; emits singular class names
and `model_config` with the table name. Generated code is a starting point — review
before commit. Does not emit credentials or row data.

---

### `ferrum resetdb`

Drop all Ferrum model tables and clear the ledger.

```bash
ferrum resetdb --confirm
ferrum resetdb --env development --confirm
```

**Requires `--confirm`.** Non-development envs print an extra warning. Table names come
only from loaded model metadata. Dialect-specific `DROP TABLE` quoting/cascade variants
apply (MySQL / SQLite / Postgres paths differ).

---

### Legacy: `ferrum migrations`

Plan-JSON API (Rust core plan files):

```bash
ferrum migrations dry-run plan.json
ferrum migrations apply plan.json --dry-run
ferrum migrations apply plan.json --confirm --environment production
ferrum migrations apply plan.json --token <digest-token>
```

| Flag                                 | Role                                                                                            |
| ------------------------------------ | ----------------------------------------------------------------------------------------------- |
| `--confirm`                          | Destructive + non-dev gate                                                                      |
| `--token` / `FERRUM_MIGRATION_TOKEN` | Plan-digest verification; supplying a token implies confirm semantics for the legacy apply path |
| `--environment`                      | Target env label                                                                                |

Prefer file-based `makemigrations` / `migrate` for application projects.

---

## 4. Typical workflows

### Greenfield (PostgreSQL)

```bash
ferrum init --name blog
cd blog
cp .env.example .env   # edit FERRUM_DATABASE_URL
docker compose up -d
# create models + ferrum_conf.py that imports them
ferrum makemigrations --name initial
ferrum migrate --dry-run
ferrum migrate
```

### Apply the same migration SQL on another dialect (preview)

```bash
ferrum sqlmigrate 0001_initial --dialect mysql
ferrum sqlmigrate 0001_initial --dialect mssql
```

Ops unsupported on that dialect fail at render/apply with `[FERR-M001]` — fix the
migration set or keep PG-only ops behind a Postgres DSN.

### Inspect an existing Postgres database

```bash
export FERRUM_DATABASE_URL=postgresql://…
ferrum inspectdb -o models_from_db.py
# review, then wire into ferrum_conf and makemigrations as needed
```

---

## 5. Security and safety gates

- Dry-run before apply for destructive work.
- `--confirm` for drops / non-development environments.
- Ledger checksums prevent silent edits to applied files.
- CLI output never prints DSNs, passwords, bound values, or row payloads.
- `init` binds Postgres to localhost only and gitignores `.env`.

---

## 6. Troubleshooting

| Symptom                                  | Likely cause                                                                          |
| ---------------------------------------- | ------------------------------------------------------------------------------------- |
| `makemigrations` writes empty / no files | Models not imported — fix `ferrum_conf.py` / `FERRUM_SETTINGS`                        |
| `FerrumConfigError` on migrate           | Missing `FERRUM_DATABASE_URL` / driver extra / native build                           |
| `inspectdb` refuses                      | Non-Postgres DSN — use PG or write models by hand                                     |
| `[FERR-M001]` on MSSQL apply             | PG-only op (`create_extension`, RLS, `VECTOR`, …) in the migration                    |
| Checksum `[FERR-M005]`                   | Applied migration file edited — revert the file or follow a deliberate repair process |
| Dotenv not loaded                        | Install `ferrum-orm[dotenv]` or export vars in the shell                              |

---

## 7. Related docs

- [Getting Started](./getting-started.md) — config, CRUD, first migration
- [Indexes Guide](./indexes.md) — `AddIndex` / FTS DDL per driver
- [Vector Guide](./vector.md) — pgvector + CLI `sqlmigrate` previews
- [API Reference](./api-reference.md) — migration ops and errors
