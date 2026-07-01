# Getting Started with Ferrum

This guide takes you from an empty environment to a working async CRUD flow against
PostgreSQL. Every example here mirrors the runnable demo in
[`examples/simple/main.py`](../examples/simple/main.py).

> Ferrum is **async-only**. Every database operation is a coroutine you `await`.
> There is no synchronous API in v0.1.

---

## 1. Prerequisites

- Python 3.11+
- PostgreSQL 14+ (the only supported database in v0.1)
- The compiled native extension (`ferrum._native`), built via maturin
- For CLI commands (`ferrum init`, `migrate`, `makemigrations`, …): install the optional
  CLI extra — `pip install 'ferrum[cli]'` or `pip install 'ferrum[cli,dotenv]'` for automatic
  `.env` loading via `ferrum.toml` or `pyproject.toml`
- The CLI optional extra for command-line tools: `pip install 'ferrum[cli]'`
  (or `ferrum[cli,dotenv]` when you also want automatic `.env` loading)

Ferrum's query engine delegates SQL compilation to a Rust core. If the extension is not
built, terminal query methods raise `FerrumConfigError` with the message:

```
ferrum._native extension not built. Run: maturin develop  (or: uv run maturin develop)
```

Build it from the repo root:

```bash
mise run dev        # project task that runs maturin develop
# or directly:
uv run maturin develop
```

---

## 2. Configure the database URL

Ferrum reads `FERRUM_DATABASE_URL` when you call `connect()` with no argument:

```bash
export FERRUM_DATABASE_URL=postgresql://ferrum:changeme@127.0.0.1:5432/ferrum_dev
```

A local PostgreSQL is provided by the example compose files
([`examples/simple/docker-compose.yml`](../examples/simple/docker-compose.yml)):

```bash
cd examples/simple
docker compose up -d
```

The DSN is **never** logged or included in hook payloads. If a connection fails, Ferrum
reports only host / port / database / username / error category — never the password or
the full DSN.

---

## 2a. Project config (`ferrum.toml`, `pyproject.toml`, and `ferrum_conf.py`)

### `ferrum.toml` or `pyproject.toml`

Ferrum reads a `[ferrum]` table from `ferrum.toml` when that file exists; otherwise
it reads the same table from `pyproject.toml`. This lets you colocate Ferrum settings
with your Python packaging metadata when you do not want a separate config file.

`ferrum init` scaffolds a dedicated `ferrum.toml` with all keys commented out.
**Secrets never go here** — database URLs and passwords belong in `.env`.

```toml
# ferrum.toml — or the [ferrum] table in pyproject.toml
[ferrum]
# Python module that imports your app's models (enables makemigrations auto-discovery)
# settings = "ferrum_conf"

# Migrations directory (default: ./migrations)
# migrations_dir = "migrations"

# Default environment name used by ferrum migrate
# default_env = "development"

# Path to dotenv file loaded by the CLI (default: .env)
# env_file = ".env"

# Environment variable for the database URL (default: FERRUM_DATABASE_URL, then DATABASE_URL)
# database_url_env = "DATABASE_URL"
```

Example in `pyproject.toml`:

```toml
[project]
name = "myapp"
version = "0.1.0"

[ferrum]
settings = "ferrum_conf"
migrations_dir = "migrations"
default_env = "development"
env_file = ".env"
```

Available keys:

| Key                | Default         | Description                                                                                                     |
| ------------------ | --------------- | --------------------------------------------------------------------------------------------------------------- |
| `settings`         | `None`          | Python module the CLI imports before running any subcommand.                                                    |
| `migrations_dir`   | `"migrations"`  | Directory (relative to project root) for migration files.                                                       |
| `default_env`      | `"development"` | Environment name used by `ferrum migrate`.                                                                      |
| `env_file`         | `".env"`        | Dotenv file loaded by the CLI (relative to project root).                                                       |
| `database_url_env` | _(see below)_   | Env var holding the database URL. When unset or empty, Ferrum tries `FERRUM_DATABASE_URL`, then `DATABASE_URL`. |

### ferrum_conf.py

`ferrum_conf.py` is the **model-import hook** — the file that tells the Ferrum CLI which
models exist so that `ferrum makemigrations` can find them. Create it in the project root:

```python
# ferrum_conf.py — loaded automatically by the Ferrum CLI
# Import all model modules here so makemigrations can find them.

import myapp.models          # registers User, Post, etc.
import myapp.auth.models     # registers Token, Session, etc.

# Optional: call a configure() hook for future extensibility
# def configure():
#     pass
```

The file is **not** scaffolded by `ferrum init` — you write it once for your project.
Top-level imports are sufficient; if you define a `configure()` callable it will be called
after the module is imported.

### CLI dotenv auto-load

The Ferrum CLI automatically loads the `.env` file (or the path set in `env_file`) from the
project root **before** running any subcommand. This means you do not need to `source .env`
before running `ferrum migrate`:

```bash
cp .env.example .env     # fill in FERRUM_DATABASE_URL
ferrum migrate           # .env is loaded automatically
```

Requirements:

- `python-dotenv` must be installed (`pip install ferrum[dotenv]` or add it to your project).
- Without `python-dotenv`, dotenv loading is silently skipped — no error.
- Already-set environment variables are **never** overridden (`override=False`), so
  CI/production values set in the shell always take precedence over `.env`.

### FERRUM_SETTINGS override

The `FERRUM_SETTINGS` environment variable overrides the settings module for the current
shell session. Useful in CI or when switching between configurations:

```bash
FERRUM_SETTINGS=myapp.test_settings ferrum makemigrations
```

Discovery order: `FERRUM_SETTINGS` env var → `[ferrum].settings` in `ferrum.toml` or
`pyproject.toml` → `ferrum_conf.py` autodiscovery → skip (silently).

---

## 3. Define a model

Models subclass `ferrum.Model`, which is a Pydantic v2 `BaseModel` with a metadata builder.
The model is the single source of truth for fields, types, and validation.

```python
import ferrum
from ferrum import Model


class Note(Model):
    id: int = 0
    body: str = ""
```

Table-name resolution: the table defaults to the snake_case class name (`Note` → `note`).
Override it explicitly with `ModelConfig`:

```python
class User(ferrum.Model):
    model_config = ferrum.ModelConfig(table="users")

    id: int
    email: str
    active: bool = True
```

The first `int` field named `id` is treated as the primary key when no explicit PK is
declared. Metadata (the table/column/operator allowlists the Rust compiler uses) is built
**once** at class-definition time and is immutable thereafter.

---

## 4. Open a connection pool

`ferrum.connect()` is an async context manager yielding an open pool. Pass a DSN, or rely
on `FERRUM_DATABASE_URL`:

```python
from ferrum import connect

async with connect() as conn:                 # uses FERRUM_DATABASE_URL
    ...

async with connect("postgresql://user@host/db", min_size=1, max_size=10) as conn:
    ...
```

The pool opens on enter and closes on exit — including on exceptions and cancellation.

---

## 5. CRUD

All terminal operations take the `conn` as their first argument. Chaining methods
(`filter`, `order_by`, `limit`, `offset`) return a new `QuerySet` and touch nothing until
you `await` a terminal coroutine.

```python
import asyncio
from ferrum import connect


async def main() -> None:
    async with connect() as conn:
        # CREATE
        note = await Note.objects.create(conn, body="Hello from Ferrum")
        print(note.id, note.body)

        # READ — exactly one row
        fetched = await Note.objects.filter(id=note.id).get(conn)

        # READ — many rows
        rows = await Note.objects.filter(id=note.id).all(conn)

        # READ — first or None
        maybe = await Note.objects.order_by("-id").first(conn)

        # COUNT
        total = await Note.objects.filter(id=note.id).count(conn)

        # UPDATE (scoped — requires a filter)
        changed = await Note.objects.filter(id=note.id).update(conn, body="Updated")

        # DELETE (scoped — requires a filter)
        deleted = await Note.objects.filter(id=note.id).delete(conn)


asyncio.run(main())
```

### Filter syntax

Django-style `field__operator=value`; a bare `field=value` means `eq`:

```python
Note.objects.filter(body="exact")            # eq
Note.objects.filter(body__icontains="ferr")  # case-insensitive contains
Note.objects.filter(id__gte=10, id__lt=100)  # range via two bounds
Note.objects.filter(id__in=[1, 2, 3])
```

Allowed operators depend on the field type and are enforced against the model's allowlist
**before** any SQL is emitted. An unknown field or unsupported operator raises
`FerrumCompileError` immediately. See [API Reference → Operators](./api-reference.md#operators-by-field-type).

### Ordering

```python
Note.objects.order_by("body")    # ASC
Note.objects.order_by("-id")     # DESC (leading minus)
```

---

## 6. The Danger API (unscoped writes)

`update()` and `delete()` **require at least one filter**. An unscoped call fails fast with
`FerrumDangerApiError` — even before a connection is touched:

```python
await Note.objects.delete(conn)   # raises FerrumDangerApiError
```

To delete or update every row, you must opt in explicitly with a deliberately verbose name:

```python
await Note.objects.danger_delete_all(conn)
await Note.objects.danger_update_all(conn, body="reset")
```

---

## 7. Observability hooks

Register a hook to observe the query lifecycle. Default (Tier A) payloads carry only
identifiers — never bound values, DSNs, or row data.

```python
from ferrum import register_hook, clear_hooks


def log_hook(payload: dict) -> None:
    print(payload["event"], payload.get("model"), payload.get("status"))


register_hook("*", log_hook)   # "*" = all events; or "query_start" / "query_success" / "query_failure"
# ... run queries ...
clear_hooks()                  # test teardown
```

Tiers B (normalized SQL) and C (full SQL + bound values) require an explicit opt-in via the
`FERRUM_OBS` environment variable and are **never** enabled by a generic `DEBUG=1`. Tier C
is local-dev only and additionally gated behind `FERRUM_OBS_ALLOW_TIER_C=1`.

---

## 8. Migrations

Ferrum migrations enforce a **mandatory dry-run → confirm → apply** sequence. The Python
orchestrator computes an additive schema plan from your models and applies it only after the
safety gates pass.

```python
import json
from ferrum import connect
from ferrum.migrations import apply, compute_plan

models = [Note, User]

async with connect() as conn:
    # 1. Compute a plan (additive diff against the live schema). {} = fresh DB.
    plan = compute_plan(models, existing_tables={})
    plan_json = json.dumps(plan)

    # 2. Dry-run (default) — prints the plan, applies nothing.
    await apply(conn, plan_json, dry_run=True)

    # 3. Apply for real in development.
    await apply(conn, plan_json, dry_run=False)
```

Safety gates (each raises `FerrumMigrationError` when unsatisfied):

- **Destructive ops** (`drop_table`, `drop_column`, `raw_sql`) require `confirm=True`.
- **Non-development environments** (`env != "development"`) require `confirm=True`.
- A **confirmation token**, when supplied together with `confirm=True`, is validated against
  the plan digest before any SQL runs; a mismatch raises `FerrumMigrationError`. A token is
  optional — the destructive and environment gates above stand on their own without one.

### CLI

Install the CLI extra before using command-line tools:

```bash
pip install 'ferrum[cli,dotenv]'
```

The same flow is available from the command line:

```bash
ferrum migrations dry-run
ferrum migrations apply plan.json --confirm --environment production
ferrum init --name myproject
```

> **CLI note:** supplying `--token` (or the `FERRUM_MIGRATION_TOKEN` environment variable)
> implies `--confirm` — the token is then validated against the plan digest. In other words,
> a valid token is an alternative to passing `--confirm` explicitly for destructive and
> non-development applies. If you want both signals required independently, pass `--confirm`
> yourself and treat the token purely as plan-digest verification.

See [`examples/migrations/`](../examples/migrations/) for a worked example.

---

## 9. FastAPI integration

`ferrum.contrib.fastapi.ferrum_lifespan` ties the pool lifecycle to the ASGI app lifespan:

```python
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from ferrum.connection import Connection
from ferrum.contrib.fastapi import ferrum_lifespan, get_ferrum_conn


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with ferrum_lifespan() as conn:
        app.state.ferrum_conn = conn
        yield


app = FastAPI(lifespan=lifespan)


@app.get("/users")
async def list_users(conn: Connection = Depends(get_ferrum_conn)) -> list[User]:
    return await User.objects.filter(active=True).all(conn)
```

Every QuerySet terminal requires the pool handle — inject it with
`Depends(get_ferrum_conn)` (or pass `conn` explicitly in scripts).
`ferrum_lifespan()` reads `FERRUM_DATABASE_URL` / `DATABASE_URL` when no
DSN is passed.

See [`examples/fastapi_quickstart/`](../examples/fastapi_quickstart/) for the full app.

---

## 10. UUID primary keys, indexes, and pgvector

### UUID primary keys

Use a `UUID` column as the primary key; Ferrum auto-injects
`DEFAULT gen_random_uuid()` at metadata build time:

```python
from uuid import UUID
from typing import Annotated

class Session(ferrum.Model):
    id: Annotated[UUID, ferrum.Field(primary_key=True)]
    user_id: int
```

For UUIDv7 server-side defaults (requires the `pg_uuidv7` extension and its
`uuidv7()` function), pass `uuid_generate="v7"`. For Python-side UUIDv7 generation,
install the optional extra
`pip install 'ferrum[uuid7]'` and use `from uuid6 import uuid7`.

### Declarative indexes

Define composite, unique, partial, or access-method-specific indexes on `Meta.indexes`:

```python
class Post(ferrum.Model):
    id: int
    author_id: int
    body: str

    class Meta:
        indexes = [
            ferrum.Index(fields=("author_id", "id")),
            ferrum.Index(fields=("body",), using="gin", where="active = true"),
        ]
```

`compute_plan` emits `add_index` ops after `create_table`.

### Vector and full-text columns

pgvector `VECTOR(n)` and full-text columns use sentinel types and declarative indexes:

```python
from ferrum.models import Field, FullTextIndex

class Document(ferrum.Model):
    id: Annotated[UUID, ferrum.Field(primary_key=True)]
    embedding: Annotated[ferrum.Vector, ferrum.Field(vector_dimensions=1536)]
    search_vector: Annotated[ferrum.TSVector, Field(fts_config="english")] | None = None
    body: str = ""

    class Meta:
        # MySQL / SQLite / SQL Server: FULLTEXT / FTS5 / catalog indexes
        full_text_indexes = [FullTextIndex(fields=("body",), config="english")]
```

KNN search uses `nearest_to`. Full-text search supports filter lookups and ranking:

```python
# Filter only
hits = await Document.objects.filter(search_vector__match="python rust orm").all(conn)

# Filter + relevance ranking (all drivers)
hits = await Document.objects.search(
    "python rust orm", field="search_vector", mode="websearch"
).limit(10).all(conn)

# Rank without implicit filter
ranked = await Document.objects.rank_by("search_vector", "rust", mode="plain").all(conn)
```

Lookup operators on `tsvector` (and indexed `text` fields): `match`, `match_phrase`,
`match_websearch`, `match_boolean`. Query strings are always bound parameters.

| Lookup suffix       | `rank_by` / `search` mode | Meaning                                      |
| ------------------- | ------------------------- | -------------------------------------------- |
| `__match`           | `plain`                   | Natural-language terms                       |
| `__match_phrase`    | `phrase`                  | Exact phrase                                 |
| `__match_websearch` | `websearch`               | Web-style quotes and `-` negation            |
| `__match_boolean`   | `boolean`                 | Boolean query syntax (`&`, `\|`, `!`, etc.) |

`.search(query, *, field, mode="plain")` combines the matching filter with
`.rank_by(field, query, mode=mode)` so results are both filtered and ordered by
relevance. `.rank_by()` alone adds an `ORDER BY` score without requiring a filter.

### Per-dialect notes

| Driver     | Column / index model                         | Filter emit                         | Ranking emit                          |
| ---------- | -------------------------------------------- | ----------------------------------- | ------------------------------------- |
| PostgreSQL | `TSVector` column + optional GIN index       | `@@ plainto_/phraseto_/websearch_to_/to_tsquery` | `ts_rank(...)`              |
| MySQL      | `FULLTEXT` index on base `text` columns      | `MATCH(cols) AGAINST(? IN … MODE)`  | same `MATCH … AGAINST` expression     |
| SQLite     | FTS5 **virtual table** + content sync        | virtual-table `MATCH`               | correlated `bm25()`                   |
| SQL Server | `CREATE FULLTEXT CATALOG` + full-text index  | `CONTAINS` / `FREETEXT`             | `CONTAINSTABLE` / `FREETEXTTABLE` JOIN |

- **PostgreSQL:** declare `TSVector` with `Field(fts_config="english")` (regconfig
  allowlist). GIN indexes on `tsvector` columns use `Meta.indexes` with `using="gin"`.
- **MySQL:** use `FullTextIndex(fields=("title", "body"))` — migrations emit
  `FULLTEXT KEY` DDL on the base table.
- **SQLite:** FTS5 uses an external-content virtual table; set
  `FullTextIndex(..., sqlite_content_table="documents")` when the shadow table name
  differs from the model table. Migrations create the virtual table and sync triggers.
- **SQL Server:** migrations may emit `CreateFullTextCatalog` before
  `CreateFullTextIndex`. Full-text indexes populate asynchronously — integration tests
  may need retry/backoff.

Optional scored search helper (mirrors `ferrum.ext.pgvector.vector_search`):

```python
from ferrum.ext.fts import scored_search

rows = await scored_search(conn, Document, "search_vector", "rust orm", limit=5)
```

When reading/writing real vector values through asyncpg, register codecs explicitly:

```python
from ferrum.ext.pgvector import register_vector_codecs

async with ferrum.connect() as conn:
    await register_vector_codecs(conn)
    ...
```

---

## 11. Error handling

All exceptions raised to your code subclass `ferrum.FerrumError` and carry a stable
`code` attribute (`FERR-XXXX`). Catch broadly or specifically:

```python
import ferrum

try:
    user = await User.objects.get(conn, id=42)
except ferrum.FerrumNotFoundError:
    ...                       # no row matched
except ferrum.FerrumError as exc:
    print(exc.code, exc)      # any Ferrum error, with its stable code
```

See [API Reference → Exceptions](./api-reference.md#exceptions) for the full taxonomy.
