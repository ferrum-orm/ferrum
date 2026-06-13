# Ferrum Documentation

Ferrum is a next-generation **async ORM for Python** with a **Rust-powered core**,
**Pydantic v2-native models**, and a **Django-inspired developer experience**. It targets
async PostgreSQL services (FastAPI / Starlette).

> **Status:** v0.1 (pre-release). PostgreSQL only. Async-only — there is no synchronous API.

## Contents

| Document | What it covers |
|----------|----------------|
| [Getting Started](./getting-started.md) | Install, define a model, connect, run CRUD, apply a migration. |
| [API Reference](./api-reference.md) | The public `ferrum` package surface — `Model`, `QuerySet`, `connect`, errors, hooks, migrations. |
| [Architecture](./architecture.md) | The Python ↔ Rust boundary, IR flow, compile/hydrate path, security model — with diagrams. |
| [Docstring Coverage](./docstring-coverage.md) | Audit of public-symbol documentation across `python/ferrum`. |

## Design pillars (one line each)

- **Python owns ergonomics + async I/O.** Public API, connection pool, transactions, hooks, migrations.
- **Rust owns the hot path.** A pure, synchronous, stateless SQL compiler and row codec — off the I/O path.
- **No raw SQL escape hatches.** Identifiers resolve only from model-metadata allowlists; values cross the boundary as bound parameters.
- **Tiered observability, safe by default.** Default hook payloads never carry bound values, DSNs, or row data.

## A taste

```python
import ferrum
from ferrum import Model, connect


class Note(Model):
    id: int = 0
    body: str = ""


async def main() -> None:
    async with connect("postgresql://user@host/db") as conn:
        note = await Note.objects.create(conn, body="Hello from Ferrum")
        fetched = await Note.objects.filter(id=note.id).get(conn)
        print(fetched.body)
```

See [Getting Started](./getting-started.md) for the full walkthrough.
