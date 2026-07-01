# Ferrum API Reference

The public, stable surface of the `ferrum` package. **Import paths from the top-level
`ferrum` namespace are stable API; internal module paths are not.**

Everything in `ferrum.__all__`:

```python
from ferrum import (
    Model, ModelConfig, QuerySet, Q,          # modeling + querying
    Field, Index, FullTextIndex, Vector, TSVector,  # field types + indexes
    connect,                               # connections
    Transaction,                           # transaction-scoped handle
    RetryPolicy,                           # explicit retry opt-in
    register_hook, clear_hooks,            # observability
    enable_metrics, get_metrics,           # in-process Tier-A metrics
    enable_opentelemetry,                  # optional OTel bridge (ferrum-orm[otel])
    MigrationResult,                       # migrations
    FerrumError, FerrumCompileError, FerrumConfigError, FerrumConnectionError,
    FerrumDatabaseError, FerrumIntegrityError, FerrumMigrationError,
    FerrumMultipleObjectsError, FerrumNotFoundError, FerrumSchemaError,
    FerrumTimeoutError,
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

| Member                            | Description                                                                                                              |
| --------------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| `objects`                         | Class-level manager; vends a fresh `QuerySet` bound to the model. Accessing it on an _instance_ raises `AttributeError`. |
| `get_metadata() -> ModelMetadata` | Returns the immutable metadata built at class definition. Raises `AttributeError` if the model declares no fields.       |

**Field → type mapping** (Python annotation → Ferrum field type):

| Python    | Ferrum type            |     | Python     | Ferrum type                                      |
| --------- | ---------------------- | --- | ---------- | ------------------------------------------------ |
| `int`     | `int` (PK → `big_int`) |     | `datetime` | `datetime`                                       |
| `str`     | `text`                 |     | `date`     | `date`                                           |
| `bool`    | `bool`                 |     | `time`     | `time`                                           |
| `float`   | `float`                |     | `UUID`     | `uuid`                                           |
| `Decimal` | `decimal`              |     | `bytes`    | `bytes`                                          |
| `dict`    | `json`                 |     | `Vector`   | `vector` (requires `Field(vector_dimensions=n)`) |
|           |                        |     | `TSVector` | `tsvector`                                       |
|           |                        |     | unknown    | `text` (fallback)                                |

`T | None` / `Optional[T]` marks the field nullable. The first `int` field named `id`
becomes the primary key when no explicit PK is set.

### `ModelConfig(*, table=None, **kwargs) -> ConfigDict`

Configuration factory extending `pydantic.ConfigDict`. `table` sets the database table name
(defaults to the snake_case class name). Other keyword arguments pass through to Pydantic.

### `class ModelMetadata` _(returned by `Model.get_metadata()`)_

Immutable, frozen dataclass — the allowlist source for the Rust compiler and migration
planner. Never carries connection info, bound values, or row data.

| Field                       | Type                    | Description                                               |
| --------------------------- | ----------------------- | --------------------------------------------------------- |
| `table_name`                | `str`                   | Resolved table name.                                      |
| `model_name`                | `str`                   | Class name.                                               |
| `fields`                    | `tuple[FieldMeta, ...]` | Per-field descriptors.                                    |
| `indexes`                   | `tuple[IndexMeta, ...]` | Declarative btree/GIN/etc. indexes.                         |
| `full_text_indexes`         | `tuple[FullTextIndexMeta, ...]` | Declarative cross-dialect FTS indexes.              |
| `allowed_sort_directions`   | `tuple[str, ...]`       | `("asc", "desc")`.                                        |
| `pk_index`                  | `int`                   | Index of the PK field.                                    |
| `to_metadata_json() -> str` | method                  | Serializes to the JSON shape the native compiler expects. |

`FieldMeta` (frozen): `name`, `column_name`, `python_type_name`, `field_type`,
`allowed_operators`, `nullable`, `pk`, plus optional `max_length`, `db_default`,
`vector_dimensions`, `db_index`, `unique`.

`IndexMeta` (frozen): `name`, `fields`, `unique`, `using`, `where`.

`FullTextIndexMeta` (frozen): `name`, `fields`, optional `config` (PostgreSQL regconfig
or language name), optional `sqlite_content_table` (external-content FTS5 source table).

### `class Index`

Declarative index for `class Meta: indexes = [...]`. Fields: `fields`, optional `name`,
`unique`, `using` (`"btree"` default; also `"gin"`, `"gist"`, `"hash"`, `"brin"`,
`"hnsw"`, `"ivfflat"`), and optional partial-index `where`.

### `class FullTextIndex`

Declarative full-text index for `class Meta: full_text_indexes = [...]`.

| Field                  | Type              | Description                                                                 |
| ---------------------- | ----------------- | --------------------------------------------------------------------------- |
| `fields`               | `tuple[str, ...]` | Base-table columns to index (required).                                     |
| `name`                 | `str \| None`     | Index / virtual-table name (auto-generated when omitted).                   |
| `config`               | `str \| None`     | PostgreSQL `regconfig` or language hint (metadata allowlist).               |
| `sqlite_content_table` | `str \| None`     | SQLite external-content source table when it differs from the model table.  |

On PostgreSQL, prefer a `TSVector` column plus a GIN index for stored vectors; on
MySQL, SQLite FTS5, and SQL Server, `FullTextIndex` drives dialect-specific migration
DDL (`FULLTEXT KEY`, FTS5 virtual table + triggers, or catalog + full-text index).

### `Field(...)`

Ferrum-specific keyword arguments include `primary_key`, `db_column`, `unique`, `db_index`,
`max_length`, `uuid_generate` (`"v4"` \| `"v7"`), `vector_dimensions` (required for
`Vector` columns), and `fts_config` (PostgreSQL `regconfig` allowlist for `TSVector`
columns). A string `default=` value is stored as a DB-side `db_default` expression.

UUID PK columns auto-receive `db_default = "gen_random_uuid()"` unless overridden.
`uuid_generate="v7"` sets `db_default = "uuidv7()"`.

---

## Querying

### `class QuerySet[M]`

Lazy, chainable, async query builder. Chaining methods return a **new** `QuerySet`
(immutable). Terminal coroutines require an open `Connection`.

#### Chaining methods (no I/O, no SQL)

| Method                                                     | Description                                                                             |
| ---------------------------------------------------------- | --------------------------------------------------------------------------------------- |
| `filter(*args, **kwargs) -> QuerySet[M]`                   | Add lookups or `Q` boolean trees (`field__operator=value`; bare `field=value` is `eq`). |
| `exclude(*args, **kwargs) -> QuerySet[M]`                  | Negated filter (`~Q(...)`).                                                             |
| `distinct() -> QuerySet[M]`                                | Emit `SELECT DISTINCT`.                                                                 |
| `only(*fields) -> QuerySet[M]`                             | Project a field subset; deferred fields raise on access.                                |
| `defer(*fields) -> QuerySet[M]`                            | Omit fields from SELECT; deferred fields raise on access.                               |
| `select_related(*relations) -> QuerySet[M]`                | FK / O2O forward relations via `LEFT JOIN` (one query).                                 |
| `prefetch_related(*relations) -> QuerySet[M]`              | Reverse FK / M2M via batched follow-up queries (N+1 → 2).                               |
| `values(*fields) -> QuerySet[M]`                           | Return dict rows from `all()` instead of model instances.                               |
| `values_list(*fields, flat=False) -> QuerySet[M]`          | Return tuple rows (or a flat list when `flat=True` and one field).                      |
| `order_by(*fields) -> QuerySet[M]`                         | `ORDER BY`; prefix a field with `-` for DESC.                                           |
| `limit(count) -> QuerySet[M]`                              | Set `LIMIT`.                                                                            |
| `offset(count) -> QuerySet[M]`                             | Set `OFFSET`.                                                                           |
| `nearest_to(field, vector, *, metric="l2") -> QuerySet[M]` | pgvector KNN ordering (`l2`, `cosine`, `inner_product`).                                |
| `rank_by(field, query, *, mode="plain") -> QuerySet[M]`    | Full-text relevance ordering (`plain`, `phrase`, `websearch`, `boolean`).               |
| `search(query, *, field, mode="plain") -> QuerySet[M]`    | Filter + rank on a full-text field in one call.                                          |
| `to_ir_json() -> str`                                      | Serialize current state to the ADR-002 v3 IR JSON string (runs allowlist checks).       |

**Full-text IR (`text_rank_by`)** — when `rank_by()` or `search()` is used, the serialized
IR includes an optional top-level node:

```json
{
  "text_rank_by": {
    "field": "search_vector",
    "query": "rust orm",
    "mode": "plain"
  }
}
```

`mode` is one of `plain`, `phrase`, `websearch`, `boolean`. The Rust compiler maps this
node to dialect-specific `ORDER BY` relevance expressions (`ts_rank`, `MATCH … AGAINST`,
`bm25()`, or `CONTAINSTABLE`/`FREETEXTTABLE`). Query strings in both the filter predicate
and `text_rank_by` are bound parameters — never interpolated into SQL.
| `qs[start:stop]`                                           | Slice shorthand for `offset` / `limit`.                                                 |

#### Terminal coroutines (require `conn: Connection`)

| Method                                                                 | Returns            | Notes                                                                         |
| ---------------------------------------------------------------------- | ------------------ | ----------------------------------------------------------------------------- |
| `await create(conn, **values)`                                         | `M`                | `INSERT … RETURNING *`, hydrates the row.                                     |
| `await bulk_create(conn, objects, *, batch_size=1000, returning=True)` | `list[M]` or `int` | Multi-row `INSERT`; `returning=False` returns inserted count.                 |
| `await bulk_update(conn, objects, fields, *, batch_size=1000)`         | `int`              | PK-keyed batched `UPDATE … FROM (VALUES …)`.                                  |
| `await bulk_delete(conn, ids, *, batch_size=1000)`                     | `int`              | PK-keyed batched `DELETE … IN (…)`.                                           |
| `await all(conn)`                                                      | `list[M]`          | All matching rows.                                                            |
| `await first(conn)`                                                    | `M \| None`        | Applies `LIMIT 1`.                                                            |
| `await get(conn, **kwargs)`                                            | `M`                | Exactly one row. Raises `FerrumNotFoundError` / `FerrumMultipleObjectsError`. |
| `await count(conn)`                                                    | `int`              | `SELECT COUNT(*)`; ignores limit/offset.                                      |
| `await exists(conn)`                                                   | `bool`             | `SELECT EXISTS(subquery)`; no row hydration.                                  |
| `await update(conn, **assignments)`                                    | `int`              | **Requires a filter.** Returns affected rows.                                 |
| `await delete(conn)`                                                   | `int`              | **Requires a filter.** Returns affected rows.                                 |
| `await danger_update_all(conn, **assignments)`                         | `int`              | Unscoped update — explicit opt-in.                                            |
| `await danger_delete_all(conn)`                                        | `int`              | Unscoped delete — explicit opt-in.                                            |

`update()` / `delete()` without any filter raise **`FerrumDangerApiError`** before touching
the connection.

#### Operators by field type

| Field type                                              | Allowed operators                                                                      |
| ------------------------------------------------------- | -------------------------------------------------------------------------------------- |
| `int`, `big_int`, `float`, `decimal`                    | `eq ne gt gte lt lte in is_null range`                                                 |
| `text`                                                  | `eq ne iexact contains icontains startswith endswith istartswith iendswith in is_null` |
| `datetime`, `date`, `time`                              | `eq ne gt gte lt lte is_null range`                                                    |
| `bool`                                                  | `eq ne is_null`                                                                        |
| `uuid`, `bytes`                                         | `eq ne in is_null`                                                                     |
| `json`                                                  | `eq is_null contains has_key has_any_keys`                                             |
| `array_text`, `array_int`, `array_uuid`, `array_float`  | `eq is_null contains contained_by overlap`                                             |
| `enum`                                                  | `eq ne in is_null`                                                                     |
| `vector`                                                | `is_null` (KNN via `nearest_to`)                                                       |
| `tsvector`                                              | `match match_phrase match_websearch match_boolean is_null`                             |
| indexed `text` (via `Meta.full_text_indexes`)           | `match match_phrase match_websearch match_boolean` (same as `tsvector`)                |

**Full-text operator SQL mapping** (query string always bound; config/index names from metadata):

| Operator          | PostgreSQL              | MySQL                         | SQLite FTS5              | SQL Server        |
| ----------------- | ----------------------- | ----------------------------- | ------------------------ | ----------------- |
| `match`           | `@@ plainto_tsquery`    | `MATCH … NATURAL LANGUAGE`    | virtual table `MATCH`    | `FREETEXT`        |
| `match_phrase`    | `@@ phraseto_tsquery`   | `MATCH … NATURAL LANGUAGE`    | virtual table `MATCH`    | `CONTAINS`        |
| `match_websearch` | `@@ websearch_to_tsquery` | `MATCH … NATURAL LANGUAGE`  | virtual table `MATCH`    | `FREETEXT`        |
| `match_boolean`   | `@@ to_tsquery`         | `MATCH … BOOLEAN MODE`        | virtual table `MATCH`    | `CONTAINS`        |

Ranking via `rank_by()` emits `ts_rank` (PG), `MATCH … AGAINST` (MySQL),
correlated `bm25()` (SQLite), or `CONTAINSTABLE`/`FREETEXTTABLE` (SQL Server).

**Array operator SQL mapping** (`field__op=value`):

| Operator       | PostgreSQL SQL emitted          | Notes                                           |
| -------------- | ------------------------------- | ----------------------------------------------- |
| `contains`     | `col @> $1`                     | Array contains all elements in `$1`             |
| `contained_by` | `col <@ $1`                     | Array is a subset of `$1`                       |
| `overlap`      | `col && $1`                     | Array shares at least one element with `$1`     |

**JSONB operator SQL mapping** (`field__op=value`):

| Operator       | PostgreSQL SQL emitted          | Notes                                           |
| -------------- | ------------------------------- | ----------------------------------------------- |
| `contains`     | `col @> $1`                     | JSONB column contains the JSON sub-document     |
| `has_key`      | `col ? $1`                      | JSONB column has top-level key `$1`             |
| `has_any_keys` | `col ?| $1`                     | JSONB column has any of the keys in array `$1`  |

An unsupported operator for a field raises `FerrumCompileError` before SQL emission.

### `class Q`

Composable boolean filter for `QuerySet.filter` / `exclude`::

```python
from ferrum import Q

await User.objects.filter(
    Q(active=True) & (Q(email__icontains="@co.com") | Q(role="admin"))
).exclude(banned=True).all(conn)
```

Supports `&` (AND), `|` (OR), and `~` (NOT). Lowered to IR v3 predicate trees and
compiled in Rust.

---

## Connections

### `connect(dsn=None, *, min_size=1, max_size=10, acquire_timeout=None, query_timeout=None, statement_timeout=None, max_lifetime=None, retry=None, drain_timeout=30.0)` _(async context manager)_

Yields an open `Connection` (asyncpg pool). If `dsn` is omitted, Ferrum reads
`FERRUM_DATABASE_URL`, then `DATABASE_URL`. Set `[ferrum].database_url_env` in
`ferrum.toml` or `pyproject.toml` to use a different env var name. If none are present,
raises `FerrumConfigError`. The pool closes on exit. The DSN is never logged or placed
in hook payloads.

Production runtime options (all optional):

| Parameter           | Description                                                               |
| ------------------- | ------------------------------------------------------------------------- |
| `acquire_timeout`   | Seconds to wait for a pooled connection (`FerrumTimeoutError` on expiry). |
| `query_timeout`     | Per-query Python-side deadline in seconds.                                |
| `statement_timeout` | Server-side `statement_timeout` in milliseconds (PostgreSQL).             |
| `max_lifetime`      | Recycle idle connections after this many seconds.                         |
| `retry`             | Explicit `RetryPolicy` — default is **no retries**.                       |
| `drain_timeout`     | Seconds to wait for in-flight queries during graceful `close()`.          |

```python
async with ferrum.connect(
    "postgresql://user@host/db",
    query_timeout=5.0,
    statement_timeout=5000,
) as conn:
    users = await User.objects.filter(active=True).all(conn)
```

### `class Connection`

A managed asyncpg pool. Usually obtained via `connect()`; can be constructed directly.

| Member                               | Description                                                                                                                                                                                |
| ------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `await open()`                       | Open the pool. Connection failures raise `FerrumConnectionError` with redacted diagnostics.                                                                                                |
| `await close()`                      | Graceful shutdown: stop accepting, drain in-flight work, then close the pool.                                                                                                              |
| `await health_check(timeout=5.0)`    | Cheap `SELECT 1` liveness probe; raises `FerrumTimeoutError` on expiry.                                                                                                                    |
| `acquire()` _(async ctx mgr)_        | Yields a raw asyncpg connection; released on exit. Honors `acquire_timeout`.                                                                                                               |
| `await release(raw_conn)`            | Manual release (prefer `acquire()`).                                                                                                                                                       |
| `transaction(...)` _(async ctx mgr)_ | Yields a `Transaction` on one pinned connection; commits on clean exit, rolls back on exception or cancellation. Optional `isolation`, `readonly`, `deferrable`, and `deadline` (seconds). |
| `async with`                         | Opens on enter, closes on exit.                                                                                                                                                            |

### `class RetryPolicy`

Explicit, opt-in retry configuration for `connect()` / `Connection`. **Default Ferrum
behavior is no retries.**

| Field          | Default        | Description                                                  |
| -------------- | -------------- | ------------------------------------------------------------ |
| `max_attempts` | `3`            | Maximum attempts including the first.                        |
| `on`           | `{"deadlock"}` | Retry categories: `deadlock`, `connection`, `serialization`. |
| `backoff_base` | `0.05`         | Base backoff seconds between attempts.                       |

```python
async with ferrum.connect(dsn, retry=ferrum.RetryPolicy(max_attempts=3, on=frozenset({"deadlock"}))) as conn:
    ...
```

### `class Transaction`

Transaction-scoped handle obtained from `Connection.transaction()`. Accepted anywhere a
`Connection` is accepted by QuerySet terminals — all statements share the pinned connection.

| Member                          | Description                                                                       |
| ------------------------------- | --------------------------------------------------------------------------------- |
| `dialect`                       | Same dialect as the parent connection (`postgres`, etc.).                         |
| `savepoint()` _(async ctx mgr)_ | Nested savepoint; rolls back independently of the enclosing transaction on error. |

```python
async with conn.transaction(isolation="serializable") as tx:
    await Account.objects.create(tx, name="alice", balance=100)
    async with tx.savepoint() as sp:
        await Account.objects.create(sp, name="bob", balance=50)  # rolled back on error
```

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

### `enable_metrics()` / `get_metrics()` / `enable_opentelemetry(...)`

Tier-A-safe in-process metrics and optional OpenTelemetry bridge (`ferrum-orm[otel]` extra).
Metrics derive only from redacted hook payloads — bound values and DSNs never exported.

```python
ferrum.enable_metrics()
# ... run queries ...
print(ferrum.get_metrics())  # ferrum.query.count, ferrum.query.duration_ms, etc.

# Optional OTel (requires ferrum-orm[otel]):
ferrum.enable_opentelemetry(tracer_provider=..., meter_provider=...)
```

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

### CLI migration commands

| Command                         | Purpose                                                                                     |
| ------------------------------- | ------------------------------------------------------------------------------------------- |
| `ferrum makemigrations`         | Autogenerate migration files from model metadata vs prior migration state (no DB).          |
| `ferrum migrate`                | Apply pending migrations; verifies checksums; warns on schema drift when models are loaded. |
| `ferrum revert [--target NAME]` | Revert last applied migration, or all applied after `--target` (exclusive).                 |
| `ferrum showmigrations`         | List migrations: `[X]` applied, `[!]` checksum mismatch, `[ ]` pending.                     |
| `ferrum sqlmigrate NAME`        | Print offline SQL for a migration file (no DB connection).                                  |

Migration files support `dependencies = [...]` (topologically sorted by the loader) and
`reverse_operations` for rollback. Checksums are `sha256(name:file_content)` stored in
`ferrum_migrations`; editing an applied file raises `FerrumMigrationError` `[FERR-M005]`.

`AlterColumn` supports PostgreSQL type/nullability/default changes (destructive when
setting `NOT NULL`).

---

## Session / RLS Helpers

Transaction-scoped GUC helpers for multi-tenant Row Level Security patterns.
All helpers require a `Transaction` obtained from `conn.transaction()`.

```python
import ferrum
import ferrum.session  # or: from ferrum import tenant_transaction, set_session_config, get_session_config
```

### `ALLOWED_GUC_NAMES`

`frozenset[str]` of GUC parameter names accepted by the session helpers:

```python
{"app.team_id", "app.platform_admin", "ferrum.tenant_id", "ferrum.admin",
 "statement_timeout", "lock_timeout", "work_mem", "application_name"}
```

Pass a name outside this set → `FerrumCompileError` (`FERR-C102`, `category="guc_name_not_allowed"`).

### `await ferrum.set_session_config(tx, name, value)` / `ferrum.session.set_config`

Set a GUC within the current transaction using `set_config(name, $1, true)`.
The `transaction_local=true` flag ensures the GUC resets when the transaction ends,
preventing pooled connections from leaking tenant state across requests.

- `name`: must be in `ALLOWED_GUC_NAMES`.
- `value`: bound parameter — never interpolated into SQL.

### `await ferrum.get_session_config(tx, name, *, missing_ok=True)` / `ferrum.session.current_setting`

Read a GUC from the current transaction context.

- Returns the setting value as `str`, or `None` when `missing_ok=True` and the setting
  is absent or empty.
- `name` must be in `ALLOWED_GUC_NAMES`.

### `async with ferrum.tenant_transaction(conn, tenant_id, *, guc_name="app.team_id", admin=False, admin_guc="app.platform_admin", isolation=None, readonly=False) as tx`

Open a transaction and bind `tenant_id` as a transaction-local GUC before yielding.

```python
async with ferrum.tenant_transaction(conn, team_id) as tx:
    rows = await Ticket.objects.filter(team_id=team_id).all(tx)

# Platform-admin path — also sets app.platform_admin = 'true':
async with ferrum.tenant_transaction(conn, team_id, admin=True) as tx:
    rows = await Ticket.objects.all(tx)
```

- `tenant_id`: `str` or `uuid.UUID` — converted to string and bound as a GUC value.
- `guc_name`: GUC to use for tenant binding (must be in `ALLOWED_GUC_NAMES`).
- `admin=True`: additionally sets `admin_guc = 'true'` for RLS bypass policies.
- All other kwargs are forwarded to `conn.transaction()`.

**Pool safety**: because both GUCs use `set_config(..., true)`, they reset
automatically on commit or rollback — the connection is always returned cleanly.

---

## Stored Procedure Calls

### `await conn.call_function(function_name, *args, schema="public")` / `await tx.call_function(...)`

Call a PostgreSQL function with bound arguments.

```python
async with conn.transaction() as tx:
    rows = await tx.call_function("purge_team_retention_data", team_id)
```

- `function_name` and `schema` are validated against `^[a-zA-Z_][a-zA-Z0-9_]{0,62}$`.
  Invalid names raise `FerrumCompileError` — never construct these from user input.
- Positional `*args` are always bound parameters (`$1`, `$2`, …).
- Emits `SELECT * FROM "schema"."function_name"($1, ...)`.
- Returns `list[dict[str, Any]]` (empty for void functions).
- Available on both `Connection` (acquires from pool) and `Transaction` (uses pinned connection).

---

## Migration Operations — Extensions, RLS, Functions

New operation classes for PostgreSQL-specific DDL. Import from `ferrum.migrations` or `ferrum`:

```python
from ferrum import (
    CreateExtension, DropExtension,
    EnableRLS, DisableRLS, CreatePolicy, DropPolicy,
    CreateFunction, DropFunction,
)
```

| Class | Classification | SQL emitted |
|---|---|---|
| `CreateExtension(name, *, schema=None)` | `non_transactional` | `CREATE EXTENSION IF NOT EXISTS "name"` |
| `DropExtension(name, *, cascade=False)` | `destructive` | `DROP EXTENSION IF EXISTS "name" [CASCADE]` |
| `EnableRLS(table_name, *, force=False)` | `safe` | `ALTER TABLE "t" ENABLE [FORCE] ROW LEVEL SECURITY` |
| `DisableRLS(table_name)` | `destructive` | `ALTER TABLE "t" DISABLE ROW LEVEL SECURITY` |
| `CreatePolicy(policy_name, table_name, using, *, check_expr=None, command="ALL", role=None)` | `safe` | `CREATE POLICY "name" ON "table" [FOR cmd] [TO role] USING (expr) [WITH CHECK (expr)]` |
| `DropPolicy(policy_name, table_name)` | `destructive` | `DROP POLICY IF EXISTS "name" ON "table"` |
| `CreateFunction(function_name, body)` | `non_transactional` | `body` emitted verbatim |
| `DropFunction(function_name, *, args="")` | `destructive` | `DROP FUNCTION IF EXISTS "name"(args)` |
| `CreateFullTextCatalog(catalog_name)` | `safe` | SQL Server full-text catalog DDL |
| `CreateFullTextIndex(table_name, index_name, columns, *, config=None, sqlite_content_table=None, catalog=None)` | `safe` | Dialect-specific full-text index DDL |
| `DropFullTextIndex(table_name, index_name)` | `destructive` | Drop full-text index / FTS5 virtual table |

**Security note**: `CreatePolicy.using`, `CreatePolicy.check_expr`, and `CreateFunction.body`
are raw SQL fragments supplied by the developer in migration files. They are emitted verbatim
and are **not** safe endpoints for user input.

**Classification gates**:
- `destructive` operations require `confirm=True` in `apply()` (MIG-2).
- `non_transactional` operations are tracked via `_NON_TRANSACTIONAL_KINDS` in `orchestrator.py`
  and will be subject to the ADR-004 non-transactional gate once that ADR is resolved.

---

## Exceptions

All subclass `FerrumError` and carry a stable `code`.

| Exception                    | Code        | Raised when                                                                                                    |
| ---------------------------- | ----------- | -------------------------------------------------------------------------------------------------------------- |
| `FerrumError`                | `FERR-0000` | Base class — catch-all.                                                                                        |
| `FerrumConfigError`          | `FERR-C001` | Missing DSN, or native extension not built.                                                                    |
| `FerrumCompileError`         | `FERR-C102` | Unknown field, unsupported operator, or IR version mismatch. Carries `model`, `field`, `operator`, `category`. |
| `FerrumNotFoundError`        | `FERR-Q404` | `get()` matched no row.                                                                                        |
| `FerrumMultipleObjectsError` | `FERR-Q405` | `get()` matched more than one row.                                                                             |
| `FerrumIntegrityError`       | `FERR-D201` | Constraint violation (unique/FK/not-null/check). Carries `constraint`, `category`.                             |
| `FerrumConnectionError`      | `FERR-E101` | Connection/pool error. Diagnostics limited to host/port/db/user.                                               |
| `FerrumTimeoutError`         | `FERR-E102` | Query/connection timed out.                                                                                    |
| `FerrumInternalError`        | `FERR-E500` | A Rust panic crossed the PyO3 boundary (sanitized category only).                                              |
| `FerrumMigrationError`       | `FERR-M001` | Migration failed or rejected by a safety gate.                                                                 |
| `FerrumDangerApiError`       | `FERR-U301` | Unscoped `delete()`/`update()` without the danger API.                                                         |
| `FerrumSchemaError`          | `FERR-S001` | Referenced table/column does not exist (SQLSTATE 42703 / 42P01).                                               |
| `FerrumDatabaseError`        | `FERR-D001` | General database error with no more specific mapping.                                                          |

> `FerrumTimeoutError`, `FerrumInternalError`, and `FerrumDangerApiError` are part of the
> taxonomy but are **not** re-exported at the top level today — import them from
> `ferrum.errors` if you need to reference them directly.

No exception message ever contains bound parameter values, DSNs, passwords, or raw
PostgreSQL `DETAIL`/`HINT` row data.

---

## Extension helpers

### `ferrum.ext.fts.scored_search(conn, model, field, query, *, mode="plain", limit=10, score_alias="score", filters=None)`

Optional helper mirroring `ferrum.ext.pgvector.vector_search`. Filters by the mode-mapped
lookup operator, orders by relevance via `rank_by()`, and returns row dicts with a
`score_alias` key. Query strings are bound; identifiers come from model metadata only.
