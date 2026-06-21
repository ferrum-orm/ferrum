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
- PostgreSQL-first architecture (MySQL, MSSQL, and SQLite via optional extras)
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

### v0.1

- [ ] PostgreSQL support
- [ ] MySQL support
- [ ] MSSQL support
- [ ] Basic CRUD operations
- [ ] Async query execution
- [ ] Pydantic models
- [ ] Query builder
- [ ] Type-safe filters

### v0.2

- [ ] Relationships
- [x] Transactions
- [ ] Query optimization
- [ ] Bulk operations

### v0.3

- [ ] Migrations
- [ ] Schema diff engine
- [ ] CLI tools

### v1.0

- [ ] Production-ready stability
- [ ] Advanced relationships
- [ ] Performance benchmarking
- [ ] Full documentation

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

# MSSQL (Microsoft SQL Server)
pip install 'ferrum-orm[mssql]'

# SQLite + migrations CLI (testing / local dev)
pip install 'ferrum-orm[sqlite,cli]'

# Everything (all drivers + CLI + dotenv)
pip install 'ferrum-orm[all]'

# Core ORM only (no database driver — install a driver extra before connecting)
pip install ferrum-orm
```

Bare `ferrum-orm` installs Pydantic and the Rust core only. Choose a driver extra
(`pg`, `mysql`, `mssql`, or `sqlite`) before calling `ferrum.connect()`.

From source, build the native extension with `maturin develop` (or `mise run dev`).

## Examples

Runnable samples live under [`examples/`](examples/):

- [`examples/simple/`](examples/simple/) — async CRUD script (no web framework)
- [`examples/migrations/`](examples/migrations/) — CLI, plan generation, apply, and forward fix-ups
- [`examples/fastapi_quickstart/`](examples/fastapi_quickstart/) — FastAPI integration

## Contributing

Contributions are welcome.

## License

Apache License 2.0
