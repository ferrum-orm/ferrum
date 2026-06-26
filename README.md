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
- PostgreSQL-first architecture (MySQL and SQLite via optional extras)
- Type-safe query construction
- Automatic migrations
- High-performance result hydration
- Production-ready observability

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

### Rust-Powered Core

Performance-critical components are implemented in Rust:

- Query compilation
- SQL generation
- Result decoding
- Schema analysis
- Migration planning

This allows Ferrum to maintain a Pythonic API without sacrificing performance.

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
- [x] Full-text search (TSVector / `plainto_tsquery`)
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
- [ ] Query optimization (deferred fields, prefetch tuning)
- [ ] Advanced relationship loading

### v1.0

- [ ] Production-ready stability
- [ ] Performance benchmarking suite
- [ ] Full documentation site

## Project Status

Ferrum is currently in active development.

The API is not yet stable and breaking changes should be expected until the first public release.

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

# Everything (all drivers + CLI + dotenv)
pip install 'ferrum-orm[all]'

# Core ORM only (no database driver — install a driver extra before connecting)
pip install ferrum-orm
```

Bare `ferrum-orm` installs Pydantic and the Rust core only. Choose a driver extra
(`pg`, `mysql`, or `sqlite`) before calling `ferrum.connect()`.

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
