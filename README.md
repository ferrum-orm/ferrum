# Ferrum

> A next-generation async ORM for Python.
> Rust-powered engine. Pydantic-native models. Django-inspired developer experience.

Ferrum is an async-first ORM designed for modern Python applications.

Built around a Rust-powered core and a Python-native API, Ferrum combines the ergonomics of Django's ORM, the type safety of Pydantic, and the performance of Rust.

## Why Ferrum?

Existing Python ORMs often force developers to choose between:

- Developer experience
- Async support
- Type safety
- Performance

Ferrum aims to provide all four.

### Goals

- Native async from day one
- Pydantic-first models
- Django-inspired ORM experience
- Rust-powered query engine
- PostgreSQL-first architecture. MySQL, SQLite, and SQL Server extras are best-effort thin parity — not production-supported and not production-readiness release gates
- Type-safe query construction
- Automatic migrations
- High-performance result hydration
- Production-ready observability

### Production-readiness execution

Ferrum is still alpha. The remaining production work for Ticket Analyzer and an
async-refactored Org AI Platform is tracked in
[the production-readiness plan](.cursor/plans/ferrum-production-readiness_6b5f422d.plan.md).
Execution is wave-based and supports parallel agents through disjoint file ownership,
per-workstream state, immutable evidence logs, and an independent verification gate.

Every task follows `Specify → Plan → Tasks → Implement`; every executor run follows
`Load → Execute → Validate executor output → Verify independently → Update state`.
See [.agent-work/production-readiness/](.agent-work/production-readiness/) for the durable
execution contract and current aggregate state.

---

## Quick Example

```python
from ferrum import Model


class User(Model):
    id: int
    email: str
    is_active: bool = True


user = await User.objects.create(
    conn,
    email="john@example.com",
)

# Insert from an instance (sentinel PK is dropped so the DB default runs)
draft = User(id=0, email="dana@example.com")
created = await User.objects.create(conn, draft)

# Persist one instance's changes by primary key
created.is_active = False
await User.objects.update_instance(conn, created, fields=["is_active"])

users = await (
    User.objects
    .filter(is_active=True)
    .order_by("-id")
    .limit(10)
    .all(conn)
)

async with conn.transaction() as tx:
    user = await User.objects.create(tx, email="jane@example.com")
    await AuditLog.objects.create(tx, user_id=user.id, action="signup")
```

---

## Features

### Async First

No synchronous compatibility layer.

Ferrum is designed around modern async Python applications.

```python
users = await User.objects.all(conn)
```

### Pydantic Native

Models are built directly on top of Pydantic.

```python
class User(Model):
    id: int
    email: str
```

No duplicate schema definitions.

### Field Options

`ferrum.Field()` accepts column-level constraints that are carried through to DDL:

| Parameter | Type | Description |
| --------- | ---- | ----------- |
| `db_default` | `str \| None` | DB-side `DEFAULT` expression (e.g. `"NOW()"`, `"''"`, `"GEN_RANDOM_UUID()"`). Token casing is normalised (`now()` → `NOW()`). |
| `nullable` | `bool \| None` | Override annotation-derived nullability. Use `nullable=False` to force `NOT NULL` even on a `T \| None`-annotated field. |
| `db_index` | `bool` | Emit a `CREATE INDEX` on this column (auto-named `idx_{table}_{field}`). |
| `unique` | `bool` | Emit a `UNIQUE` constraint. |
| `db_column` | `str \| None` | Override the database column name. |
| `primary_key` | `bool` | Declare this field as the primary key. |
| `uuid_generate` | `"v4" \| "v7" \| None` | Set `GEN_RANDOM_UUID()` / `UUIDV7()` as the DB default for UUID PK columns. |
| `max_length` | `int \| None` | Emit `VARCHAR(n)` instead of `TEXT`. |
| `max_digits` / `decimal_places` | `int \| None` | Emit `NUMERIC(p,s)`. |
| `vector_dimensions` | `int \| None` | Required for `Vector` columns; emits `VECTOR(n)`. |
| `jsonb_list` | `bool` | Store `list[T]` as JSONB instead of a PostgreSQL ARRAY. |
| `generated` | `bool` | Select/hydrate the DB-generated column, but exclude it from writes. |
| `read_only` | `bool` | Select/hydrate the column, but reject explicit write assignments. |

```python
from datetime import datetime
from typing import Annotated
from ferrum import Model, Field

class Event(Model):
    id: int
    # DB-side timestamp default; Python annotation allows None but column is NOT NULL
    created_at: Annotated[datetime | None, Field(db_default="now()", nullable=False)]
    # Indexed text column
    kind: Annotated[str, Field(db_index=True, max_length=64)]
```

`makemigrations` honours these field attributes:
- `db_default` / `nullable` are written into `CREATE TABLE` and `ADD COLUMN` DDL.
- `db_index=True` emits `CREATE INDEX` after the table op.
- For **already-existing tables**, `makemigrations` now **autodiffs** index and column
  attribute changes against the state projected from prior migration files:
  - Adding `db_index=True` to an existing field → `AddIndex` op.
  - Removing `db_index=True` → `DropIndex` op.
  - Adding or changing `db_default` → `AlterColumn … SET DEFAULT …` op.
  - Removing `db_default` → `AlterColumn … DROP DEFAULT` op.
  - Changing nullability → `AlterColumn … SET / DROP NOT NULL` op (SET NOT NULL is
    classified destructive and requires `--confirm`).
  - Column type changes, renames, and drops remain manual (out of scope for v0.1).

### Django-Inspired API

Familiar query interface.

```python
users = await (
    User.objects
    .filter(email__contains="@gmail.com")
    .order_by("-created_at")
    .all(conn)
)
```

Typed aggregates and bounded PostgreSQL streaming use the same metadata allowlists
and bound-parameter compiler path:

```python
from ferrum.queryset import Aggregate

counts = await (
    Ticket.objects
    .group_by("team_id")
    .date_trunc("created_at", "day", alias="day")
    .having(total__gte=10)
    .aggregate(conn, total=Aggregate.count())
)

async with Ticket.objects.filter(active=True).stream(conn, chunk_size=500) as chunks:
    async for tickets in chunks:
        await process(tickets)
```

`stream()` preserves projection/deferred/value-queryset materialization and
deterministically releases its cursor on exhaustion, early break, cancellation, or
error. It rejects `prefetch_related()` because cross-chunk relationship batching is
not safe. Filtered `update_returning()` provides atomic compare-and-set semantics:
encode the expected state in `filter()` and treat an empty returned list as a lost race.

### First-Class IDE Support

Ferrum ships a PEP 561 `py.typed` marker, so editors and type checkers (`mypy`, `pyright`,
`ty`) resolve its inline hints out of the box. `Model.objects` is typed as
`QuerySet[YourModel]`, chaining preserves the model type, and terminals infer precise results —
no casts:

```python
users: list[User] = await User.objects.filter(is_active=True).all(conn)   # list[User]
user: User | None = await User.objects.first(conn)                        # User | None
rows: list[dict[str, Any]] = await User.objects.values("id", "email").all(conn)
ids: list[Any] = await User.objects.values_list("id", flat=True).all(conn)
```

`values()` / `values_list()` return dedicated `ValuesQuerySet`, `ValuesListQuerySet`, and
`FlatValuesListQuerySet` variants (all exported from `ferrum`) so `all()` returns
`list[dict[str, Any]]`, `list[tuple[Any, ...]]`, or `list[Any]` respectively.

### Rust-Powered Core

Performance-critical components are implemented in Rust:

- Query compilation
- SQL generation
- Result decoding
- Schema analysis
- Migration planning

This allows Ferrum to maintain a Pythonic API without sacrificing performance.

### Cross-Driver Full-Text Search

Native full-text search across PostgreSQL, MySQL, SQLite FTS5, and SQL Server — one
QuerySet API, dialect-specific SQL emit and migration DDL. Cross-dialect FTS on
MySQL / SQLite / SQL Server is a **thin-extra** capability, not a P0
production-readiness release gate. PostgreSQL FTS is the production-readiness target.

**Query modes** (filter lookups and ranking):

| Mode        | Lookup operator   | Typical use                          |
| ----------- | ----------------- | ------------------------------------ |
| `plain`     | `__match`         | Natural-language terms               |
| `phrase`    | `__match_phrase`  | Exact phrase                         |
| `websearch` | `__match_websearch` | Web-style quotes, `-` negation   |
| `boolean`   | `__match_boolean` | Boolean operators (`&`, `\|`, `!`) |

**Convenience methods:**

```python
# Filter + relevance ranking in one call
hits = await Article.objects.search(
    "python async orm", field="body", mode="websearch"
).limit(10).all(conn)

# Rank without an implicit filter
ranked = await Article.objects.rank_by("body", "rust", mode="plain").all(conn)
```

**Index declaration** — PostgreSQL uses `TSVector` columns; other drivers index base
`text` columns via `Meta.full_text_indexes`:

```python
from ferrum.models import Field, FullTextIndex

class Article(Model):
    search_vector: Annotated[TSVector, Field(fts_config="english")] | None = None
    body: str = ""

    class Meta:
        full_text_indexes = [FullTextIndex(fields=("body",), config="english")]
```

Query strings are always bound parameters; `fts_config` and index names come from
model-metadata allowlists only. See [Getting Started → Vector and full-text columns](docs/getting-started.md)
and [API Reference](docs/api-reference.md) for per-dialect DDL and operator mapping.

## Architecture

```text
┌──────────────────────────┐
│      Python API          │
│  Models / QuerySets      │
└────────────┬─────────────┘
             │
             ▼
┌──────────────────────────┐
│      Ferrum Core         │
│      (Rust Engine)       │
├──────────────────────────┤
│ Query Compiler           │
│ SQL AST                  │
│ Result Decoder           │
│ Migration Planner        │
└────────────┬─────────────┘
             │
             ▼
┌──────────────────────────┐
│       PostgreSQL         │
└──────────────────────────┘
```

## Roadmap

### v0.1 (complete)

- [x] PostgreSQL support
- [x] Basic CRUD operations
- [x] Async query execution
- [x] Pydantic models
- [x] Query builder
- [x] Type-safe filters
- [x] Transactions and savepoints
- [x] Bulk operations (`bulk_create`, `bulk_update`, `bulk_delete`)
- [x] Migrations (schema diff, apply, revert, CLI)
- [x] Relationships (ForeignKey, OneToOne, ManyToMany)
- [x] pgvector KNN search and HNSW/IVFFLAT index DDL
- [x] Full-text search (PostgreSQL is the P0 gate; MySQL/SQLite/SQL Server FTS is thin-extra)
- [x] Observability hooks (Tier A/B/C)
- [x] CLI (`makemigrations`, `migrate`, `revert`, `showmigrations`, `inspectdb`, `resetdb`)

### v0.2 (in progress)

- [x] Upsert API (`upsert`, `bulk_upsert` with conflict targets and `RETURNING`)
- [x] Composite primary keys
- [x] Array field types (`uuid[]`, `text[]`, scalar arrays)
- [x] JSONB operators (`__contains`, `__has_key`)
- [x] RLS / tenant session helpers (`set_config`, `tenant_session`)
- [x] `call_function` for allowlisted stored-procedure calls
- [x] Migration ops for extensions, RLS policies, and function DDL
- [x] pgvector similarity score projection (`vector_search` helper)
- [x] `Field(db_default=..., nullable=...)` — first-class DB-side defaults and nullability override
- [x] `makemigrations` autodiff for index / default / nullability changes on existing tables
- [x] Typed aggregates, filtered `UPDATE … RETURNING`, and bounded QuerySet streaming
- [x] JSONB-list/generated/read-only metadata and read-only PostgreSQL schema drift reports
- [ ] Query optimization (deferred fields, prefetch tuning)
- [ ] Advanced relationship loading

### v1.0

- [ ] Production-ready stability
- [ ] Performance benchmarking suite
- [ ] Full documentation site

## Project Status

Ferrum is currently in active development.

The API is not yet stable. **0.x has no compatibility guarantee**: SemVer MINOR and PATCH
may both break. After 1.0, public API and migration-ops breaks require a deprecation
window of at least one minor release and 90 days, whichever is longer. Generated SQL is
never a compatibility surface. Compatibility policy: `AGENTS.md` §5a. Thin extras:
`AGENTS.md` §2.6. See also `CHANGELOG.md`.

## Installation

```bash
# PostgreSQL (most common)
pip install 'ferrum-orm[pg]'

# PostgreSQL + migrations CLI
pip install 'ferrum-orm[pg,cli]'

# MySQL
pip install 'ferrum-orm[mysql]'

# SQLite + migrations CLI (testing / local dev)
pip install 'ferrum-orm[sqlite,cli]'

# SQL Server (also needs a system ODBC driver, e.g. msodbcsql18)
pip install 'ferrum-orm[mssql]'

# Optional MessagePack wire format for the Python<->Rust boundary
pip install 'ferrum-orm[msgpack]'

# Everything (all drivers + CLI + dotenv)
pip install 'ferrum-orm[all]'

# Core ORM only (no database driver — install a driver extra before connecting)
pip install ferrum-orm
```

Bare `ferrum-orm` installs Pydantic and the Rust core only. Choose a driver extra
(`pg`, `mysql`, `sqlite`, or `mssql`) before calling `ferrum.connect()`.

MySQL, SQLite, and SQL Server are **best-effort thin-parity** backends: they are **not
production-supported** and are **not production-readiness release gates**. Core CRUD is
exercised across all four drivers; advanced behavior is capability-gated rather than
emulated with incompatible SQL. The integration-suite registry
(`tests/python/integration/backends.py`) is the coverage matrix — what is tested,
not a stability or production-support claim. PostgreSQL claims every capability.
MySQL and SQL Server claim full-text search and composite primary keys only.
SQLite claims `RETURNING`, full-text search, and composite primary keys.

| Capability | PostgreSQL | MySQL | SQLite | SQL Server |
| ---------- | :--------: | :---: | :----: | :--------: |
| Transactions | ✅ | — | — | — |
| Savepoints | ✅ | — | — | — |
| Cursor streaming | ✅ | — | — | — |
| Bulk update | ✅ | — | — | — |
| Aggregates / grouping | ✅ | — | — | — |
| Upsert | ✅ | — | — | — |
| Returning rows | ✅ | — | ✅ | — |
| Row-level security helpers | ✅ | — | — | — |
| pgvector | ✅ | — | — | — |
| Full-text search | ✅ | ✅ | ✅ | ✅ |
| JSON operators | ✅ | — | — | — |
| Case-insensitive `ILIKE` | ✅ | — | — | — |
| Array columns | ✅ | — | — | — |
| Alter column | ✅ | — | — | — |
| Composite primary keys | ✅ | ✅ | ✅ | ✅ |
| Stored-function calls | ✅ | — | — | — |

Unsupported capabilities fail with a typed Ferrum error before backend SQL is
executed. Transactions, savepoints, aggregates, upsert, alter-column, and
stored-function calls are PostgreSQL-only in this matrix; thin extras raise at
the Python/compiler boundary rather than sending Postgres SQL. Pull requests
exercise PostgreSQL and SQLite, while the nightly integration matrix runs
PostgreSQL, MySQL, SQLite, and SQL Server independently. SQLite on PRs is CI
coverage, not a production-readiness gate. Cross-dialect FTS in the matrix is
thin-extra coverage, not a P0 release gate.

SQL Server connects via `aioodbc`/`pyodbc` and requires a system ODBC driver such
as `msodbcsql18`; DSNs use the `mssql://` or `sqlserver://` scheme.

### Wire format (advanced)

The Python↔Rust IR/hydration boundary defaults to JSON. Installing the `msgpack`
extra lets you switch it to MessagePack, selected via the `FERRUM_WIRE_FORMAT`
environment variable (`json` | `msgpack`) or the `[ferrum] wire_format` key in
`ferrum.toml` / `pyproject.toml`. JSON remains the default; MessagePack is opt-in.

From source, build the native extension with `maturin develop` (or `mise run dev`).

## Examples

Runnable samples live under [`examples/`](examples/):

- [`examples/simple/`](examples/simple/) — async CRUD script (no web framework)
- [`examples/migrations/`](examples/migrations/) — CLI, plan generation, apply, and forward fix-ups
- [`examples/fastapi_quickstart/`](examples/fastapi_quickstart/) — FastAPI integration

## Contributing

Contributions are welcome. Start with [`CONTRIBUTING.md`](CONTRIBUTING.md) for local setup,
scoped verification, architecture rules, and pull request expectations.

## License

Apache License 2.0
