# Ferrum API Reference

The public, stable surface of the `ferrum` package. **Import paths from the top-level
`ferrum` namespace are stable API; internal module paths are not.**

Everything in `ferrum.__all__`:

```python
from ferrum import (
    Model, ModelConfig, QuerySet,          # modeling + querying
    connect,                               # connections
    register_hook, clear_hooks,            # observability
    MigrationResult,                       # migrations
    FerrumError, FerrumCompileError, FerrumConfigError, FerrumConnectionError,
    FerrumDatabaseError, FerrumIntegrityError, FerrumMigrationError,
    FerrumMultipleObjectsError, FerrumNotFoundError, FerrumSchemaError,
)
```

---

## Models

### `class Model`

Base class for all Ferrum models. Subclass `pydantic.BaseModel` with a metadata builder
that runs once at class-definition time.

```python
class User(ferrum.Model):
    model_config = ferrum.ModelConfig(table="users")
    id: int
    email: str
    active: bool = True
```

Default `model_config`: `validate_assignment=True`, `extra="forbid"` (schema drift surfaces
early).

**Class attributes / methods**

| Member | Description |
|--------|-------------|
| `objects` | Class-level manager; vends a fresh `QuerySet` bound to the model. Accessing it on an *instance* raises `AttributeError`. |
| `get_metadata() -> ModelMetadata` | Returns the immutable metadata built at class definition. Raises `AttributeError` if the model declares no fields. |

**Field → type mapping** (Python annotation → Ferrum field type):

| Python | Ferrum type | | Python | Ferrum type |
|--------|-------------|---|--------|-------------|
| `int` | `int` (PK → `big_int`) | | `datetime` | `datetime` |
| `str` | `text` | | `date` | `date` |
| `bool` | `bool` | | `time` | `time` |
| `float` | `float` | | `UUID` | `uuid` |
| `Decimal` | `decimal` | | `bytes` | `bytes` |
| `dict` | `json` | | unknown | `text` (fallback) |

`T | None` / `Optional[T]` marks the field nullable. The first `int` field named `id`
becomes the primary key when no explicit PK is set.

### `ModelConfig(*, table=None, **kwargs) -> ConfigDict`

Configuration factory extending `pydantic.ConfigDict`. `table` sets the database table name
(defaults to the snake_case class name). Other keyword arguments pass through to Pydantic.

### `class ModelMetadata` *(returned by `Model.get_metadata()`)*

Immutable, frozen dataclass — the allowlist source for the Rust compiler and migration
planner. Never carries connection info, bound values, or row data.

| Field | Type | Description |
|-------|------|-------------|
| `table_name` | `str` | Resolved table name. |
| `model_name` | `str` | Class name. |
| `fields` | `tuple[FieldMeta, ...]` | Per-field descriptors. |
| `allowed_sort_directions` | `tuple[str, ...]` | `("asc", "desc")`. |
| `pk_index` | `int` | Index of the PK field. |
| `to_metadata_json() -> str` | method | Serializes to the JSON shape the native compiler expects. |

`FieldMeta` (frozen): `name`, `column_name`, `python_type_name`, `field_type`,
`allowed_operators`, `nullable`, `pk`.

---

## Querying

### `class QuerySet[M]`

Lazy, chainable, async query builder. Chaining methods return a **new** `QuerySet`
(immutable). Terminal coroutines require an open `Connection`.

#### Chaining methods (no I/O, no SQL)

| Method | Description |
|--------|-------------|
| `filter(**kwargs) -> QuerySet[M]` | Add `field__operator=value` lookups (bare `field=value` is `eq`). Field names validated against the allowlist at call time. |
| `order_by(*fields) -> QuerySet[M]` | `ORDER BY`; prefix a field with `-` for DESC. |
| `limit(count) -> QuerySet[M]` | Set `LIMIT`. |
| `offset(count) -> QuerySet[M]` | Set `OFFSET`. |
| `to_ir_json() -> str` | Serialize current state to the ADR-002 v1 IR JSON string (runs allowlist checks). |

#### Terminal coroutines (require `conn: Connection`)

| Method | Returns | Notes |
|--------|---------|-------|
| `await create(conn, **values)` | `M` | `INSERT … RETURNING *`, hydrates the row. |
| `await all(conn)` | `list[M]` | All matching rows. |
| `await first(conn)` | `M \| None` | Applies `LIMIT 1`. |
| `await get(conn, **kwargs)` | `M` | Exactly one row. Raises `FerrumNotFoundError` / `FerrumMultipleObjectsError`. |
| `await count(conn)` | `int` | `SELECT COUNT(*)`; ignores limit/offset. |
| `await update(conn, **assignments)` | `int` | **Requires a filter.** Returns affected rows. |
| `await delete(conn)` | `int` | **Requires a filter.** Returns affected rows. |
| `await danger_update_all(conn, **assignments)` | `int` | Unscoped update — explicit opt-in. |
| `await danger_delete_all(conn)` | `int` | Unscoped delete — explicit opt-in. |

`update()` / `delete()` without any filter raise **`FerrumDangerApiError`** before touching
the connection.

#### Operators by field type

| Field type | Allowed operators |
|------------|-------------------|
| `int`, `big_int`, `float`, `decimal` | `eq ne gt gte lt lte in is_null range` |
| `text` | `eq ne iexact contains icontains startswith endswith istartswith iendswith in is_null` |
| `datetime`, `date`, `time` | `eq ne gt gte lt lte is_null range` |
| `bool` | `eq ne is_null` |
| `uuid`, `bytes` | `eq ne in is_null` |
| `json` | `eq is_null` |

An unsupported operator for a field raises `FerrumCompileError` before SQL emission.

---

## Connections

### `connect(dsn=None, *, min_size=1, max_size=10)` *(async context manager)*

Yields an open `Connection` (asyncpg pool). If `dsn` is omitted, `FERRUM_DATABASE_URL` is
used; if neither is present, raises `FerrumConfigError`. The pool closes on exit. The DSN is
never logged or placed in hook payloads.

```python
async with ferrum.connect("postgresql://user@host/db") as conn:
    users = await User.objects.filter(active=True).all(conn)
```

### `class Connection`

A managed asyncpg pool. Usually obtained via `connect()`; can be constructed directly.

| Member | Description |
|--------|-------------|
| `await open()` | Open the pool. Connection failures raise `FerrumConnectionError` with redacted diagnostics. |
| `await close()` | Close the pool. |
| `acquire()` *(async ctx mgr)* | Yields a raw asyncpg connection; released on exit. Driver errors mapped to the Ferrum taxonomy. |
| `await release(raw_conn)` | Manual release (prefer `acquire()`). |
| `async with` | Opens on enter, closes on exit. |

---

## Observability hooks

Three-tier model. Default is **Tier A** — identifiers only, never bound values / DSN / rows.

### `register_hook(event, fn)`

Register `fn(payload: dict)` for an event. `event` ∈ `{"query_start", "query_success",
"query_failure", "*"}` (`"*"` = all). Hooks run synchronously on the query path — keep them
fast. A crashing hook is suppressed and never breaks the query.

### `clear_hooks()`

Remove all registered hooks. Intended for test teardown.

**Tier A payload keys:** `event`, `model`, `table`, `operation`, `fingerprint`,
`duration_ms`, `status`, `failure_category`, `rows_affected`.

**Tier selection** (`FERRUM_OBS` env var): `A` (default) · `B` adds `sql_normalized` ·
`C` adds `sql_text` + `bound_params` (requires `FERRUM_OBS_ALLOW_TIER_C=1`, local-dev only).
`DEBUG=1` never elevates the tier.

---

## Migrations

`from ferrum.migrations import apply, compute_plan, MigrationResult`

### `compute_plan(model_classes, existing_tables) -> dict`

Additive-only schema diff. Emits `create_table` for absent tables and `add_column` for
new columns. `existing_tables` maps table name → list of existing column names (`{}` for a
fresh DB). Identifiers come only from model metadata. (Column type changes, renames, and
drops are out of scope in v0.1.)

### `await apply(conn, plan_json, *, dry_run=True, confirm=False, env="development", token=None) -> MigrationResult`

Apply (or dry-run) a plan JSON. Safety gates, each raising `FerrumMigrationError`:

- `dry_run=True` (default) prints the plan and applies nothing.
- Destructive ops (`drop_table`/`drop_column`/`raw_sql`) require `confirm=True` — the gate
  independently scans ops and never trusts the plan's own `requires_confirmation` flag.
- `env != "development"` requires `confirm=True`.
- A `token`, when supplied together with `confirm=True`, is validated against the plan
  digest before any SQL runs; a mismatch raises `FerrumMigrationError`. A token is optional
  and is not required on the standard apply path.

### `class MigrationResult`

`applied: bool` · `ops_count: int` · `dry_run: bool`.

---

## Exceptions

All subclass `FerrumError` and carry a stable `code`.

| Exception | Code | Raised when |
|-----------|------|-------------|
| `FerrumError` | `FERR-0000` | Base class — catch-all. |
| `FerrumConfigError` | `FERR-C001` | Missing DSN, or native extension not built. |
| `FerrumCompileError` | `FERR-C102` | Unknown field, unsupported operator, or IR version mismatch. Carries `model`, `field`, `operator`, `category`. |
| `FerrumNotFoundError` | `FERR-Q404` | `get()` matched no row. |
| `FerrumMultipleObjectsError` | `FERR-Q405` | `get()` matched more than one row. |
| `FerrumIntegrityError` | `FERR-D201` | Constraint violation (unique/FK/not-null/check). Carries `constraint`, `category`. |
| `FerrumConnectionError` | `FERR-E101` | Connection/pool error. Diagnostics limited to host/port/db/user. |
| `FerrumTimeoutError` | `FERR-E102` | Query/connection timed out. |
| `FerrumInternalError` | `FERR-E500` | A Rust panic crossed the PyO3 boundary (sanitized category only). |
| `FerrumMigrationError` | `FERR-M001` | Migration failed or rejected by a safety gate. |
| `FerrumDangerApiError` | `FERR-U301` | Unscoped `delete()`/`update()` without the danger API. |
| `FerrumSchemaError` | `FERR-S001` | Referenced table/column does not exist (SQLSTATE 42703 / 42P01). |
| `FerrumDatabaseError` | `FERR-D001` | General database error with no more specific mapping. |

> `FerrumTimeoutError`, `FerrumInternalError`, and `FerrumDangerApiError` are part of the
> taxonomy but are **not** re-exported at the top level today — import them from
> `ferrum.errors` if you need to reference them directly.

No exception message ever contains bound parameter values, DSNs, passwords, or raw
PostgreSQL `DETAIL`/`HINT` row data.
