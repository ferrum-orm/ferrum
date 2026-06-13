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
from fastapi import FastAPI
from ferrum.contrib.fastapi import ferrum_lifespan


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with ferrum_lifespan(database_url=app.state.db_url):
        yield


app = FastAPI(lifespan=lifespan)
```

See [`examples/fastapi_quickstart/`](../examples/fastapi_quickstart/) for the full app.

---

## 10. Error handling

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
