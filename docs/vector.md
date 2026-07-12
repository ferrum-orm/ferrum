# Vector Guide

pgvector support in Ferrum: model fields, codecs, KNN queries, scored search helpers,
and ANN indexes.

> **Scope:** vector columns and KNN are **PostgreSQL + pgvector only**. MySQL, SQLite,
> and SQL Server thin-parity backends do **not** map `VECTOR(n)` types — DDL fails with
> `[FERR-M001]` on MSSQL, and `register_vector_codecs` / `vector_search` raise
> `FerrumConfigError` unless `conn.dialect == "postgres"`.

---

## 1. Prerequisites

1. PostgreSQL with the [pgvector](https://github.com/pgvector/pgvector) extension available.
2. Install the driver extra: `pip install 'ferrum-orm[pg]'` (asyncpg).
3. Create the extension (migration or startup):

```python
from ferrum import migrations as ops

ops.CreateExtension("vector")
```

Or at app startup via the helper (also registers asyncpg codecs — see §3):

```python
from ferrum.ext.pgvector import register_vector_codecs

async with ferrum.connect() as conn:
    await register_vector_codecs(conn)
```

---

## 2. Model definition

Use the `Vector` sentinel with **required** `vector_dimensions`:

```python
from typing import Annotated
from uuid import UUID

import ferrum
from ferrum import Field, Model, Vector


class Document(Model):
    id: Annotated[UUID, Field(primary_key=True)]
    title: str = ""
    embedding: Annotated[Vector, Field(vector_dimensions=1536)]
```

- Python type at the boundary is `list[float]` (Pydantic list-of-float schema).
- DDL type is `VECTOR(n)` where `n` comes from `Field(vector_dimensions=n)`.
- Omitting `vector_dimensions` raises at model definition time.
- Allowed filter operators on vector fields: **`is_null` only**. Similarity / KNN is
  expressed via `nearest_to()` or `ferrum.ext.pgvector.vector_search`, not `__eq` /
  range lookups.

---

## 3. Register asyncpg codecs

Ferrum does **not** auto-register the `vector` type codec. After `connect()`, call:

```python
from ferrum.ext.pgvector import register_vector_codecs

async with ferrum.connect() as conn:
    await register_vector_codecs(conn, timeout=5.0)
    # now create / update / fetch rows with list[float] embeddings
```

Behavior:

- Runs `CREATE EXTENSION IF NOT EXISTS vector` (idempotent).
- Registers text-format encode/decode (`[1.0,2.0,…]` ↔ `list[float]`).
- Safe to call concurrently: swallows `DuplicateObject` / codec re-registration races.
- Raises `FerrumConfigError` if the connection is not PostgreSQL or the pool is closed.

**Failure modes:** missing extension privileges, pool not open, or non-PG dialect →
config error before queries run. Codec registration is pool-scoped; call once per
process/lifespan, not per request.

---

## 4. Writing and reading vectors

```python
vec = [0.01] * 1536  # same length as vector_dimensions

doc = await Document.objects.create(
    conn,
    id=uuid4(),
    title="hello",
    embedding=vec,
)

fetched = await Document.objects.get(conn, id=doc.id)
assert isinstance(fetched.embedding, list)
```

Dimension mismatches are enforced by PostgreSQL / pgvector at insert time (integrity /
database errors mapped through Ferrum's sanitized taxonomy — no raw DETAIL leaks).

---

## 5. KNN via `QuerySet.nearest_to`

```python
hits = (
    await Document.objects
    .filter(title__icontains="orm")
    .nearest_to("embedding", query_vec, metric="cosine")
    .limit(10)
    .all(conn)
)
```

| Argument | Description                                        |
| -------- | -------------------------------------------------- |
| `field`  | Name of a `Vector` field on the model              |
| `vector` | Query embedding as `list[float]` (bound parameter) |
| `metric` | `"l2"` (default), `"cosine"`, or `"inner_product"` |

IR node: `vector_order_by` → compiled to `ORDER BY col <-> $n` / `<=>` / `<#>`.

**Constraints:**

- Non-vector fields raise `FerrumCompileError`.
- Cannot combine `nearest_to()` with `rank_by()` / `search()` on the same queryset.
- Values / only / defer projections that conflict with KNN ordering raise at terminal
  compile time.

### Metric operators (PostgreSQL)

| Metric          | Operator | Typical index opclass |
| --------------- | -------- | --------------------- |
| `l2`            | `<->`    | `vector_l2_ops`       |
| `cosine`        | `<=>`    | `vector_cosine_ops`   |
| `inner_product` | `<#>`    | `vector_ip_ops`       |

---

## 6. Scored search helper

When you need an explicit similarity **score column** (not just ordering):

```python
from ferrum.ext.pgvector import vector_search

rows = await vector_search(
    conn,
    Document,
    "embedding",
    query_vec,
    metric="cosine",
    limit=10,
    score_alias="score",
    filters={"title": "hello"},  # equality filters only; keys allowlisted
)
# each row is a dict: model columns + "score"
```

Score expressions (bound query vector as `$1`):

| Metric          | Score formula (higher ≈ more similar) |
| --------------- | ------------------------------------- |
| `cosine`        | `1 - (col <=> $1::vector)`            |
| `l2`            | `1 / (1 + (col <-> $1::vector))`      |
| `inner_product` | `-(col <#> $1::vector)`               |

Identifiers come from metadata; the query vector and filters are bound parameters.
Unknown metrics / fields → `FerrumCompileError` (`[FERR-C102]`). Non-Postgres →
`FerrumConfigError`.

---

## 7. ANN indexes (HNSW / IVFFlat)

Exact KNN works without an ANN index but scans; production workloads should add
HNSW (or IVFFlat) after `CreateExtension("vector")`.

Declarative `Meta.indexes` supports `using="hnsw"` / `"ivfflat"`. For opclasses,
prefer an explicit migration op today:

```python
from ferrum import migrations as ops

operations = [
    ops.CreateExtension("vector"),
    ops.CreateTable(
        "documents",
        [
            ops.Column("id", "UUID", not_null=True, primary_key=True),
            ops.Column("embedding", "vector(1536)"),
        ],
    ),
    ops.AddIndex(
        "documents",
        "idx_documents_emb_hnsw",
        ["embedding"],
        using="hnsw",
        opclasses=["vector_cosine_ops"],
    ),
]
```

**Index build cost / concurrency:** HNSW build is CPU- and memory-heavy and holds
locks under Ferrum's default transactional `CREATE INDEX`. Build offline or in a
maintenance window for large tables. Match the opclass to the metric you query with
(`cosine` → `vector_cosine_ops`, etc.) — mismatch still runs but may not use the index
efficiently.

See [Indexes Guide](./indexes.md) for access-method allowlists and per-driver DDL.

---

## 8. Per-driver matrix

| Capability                  | PostgreSQL + pgvector | MySQL                   | SQLite        | MSSQL                  |
| --------------------------- | --------------------- | ----------------------- | ------------- | ---------------------- |
| `Vector` / `VECTOR(n)` DDL  | Yes                   | No (not in thin parity) | No            | Rejected `[FERR-M001]` |
| `CreateExtension("vector")` | Yes                   | N/A                     | N/A           | Unsupported op kind    |
| `register_vector_codecs`    | Yes (asyncpg)         | Raises                  | Raises        | Raises                 |
| `nearest_to` / IR KNN       | Yes                   | Not supported           | Not supported | Not supported          |
| `vector_search`             | Yes                   | Raises                  | Raises        | Raises                 |
| HNSW / IVFFlat indexes      | Yes                   | No                      | No            | No                     |

Install extras:

| Driver     | Extra                | Typical DSN scheme            |
| ---------- | -------------------- | ----------------------------- |
| PostgreSQL | `ferrum-orm[pg]`     | `postgresql://…`              |
| MySQL      | `ferrum-orm[mysql]`  | `mysql://…`                   |
| SQLite     | `ferrum-orm[sqlite]` | `sqlite:///…`                 |
| SQL Server | `ferrum-orm[mssql]`  | `mssql://…` / `sqlserver://…` |

---

## 9. Observability and security

- Default Tier A hooks never include the embedding values (bound params are Tier C /
  local-dev opt-in only).
- Errors never echo the full vector payload.
- Filter keys in `vector_search` must be model fields — unknown keys fail before SQL.

---

## 10. Minimal end-to-end

```python
import ferrum
from ferrum.ext.pgvector import register_vector_codecs, vector_search
from typing import Annotated
from uuid import UUID, uuid4
from ferrum import Field, Model, Vector


class Chunk(Model):
    id: Annotated[UUID, Field(primary_key=True)]
    embedding: Annotated[Vector, Field(vector_dimensions=8)]


async def main() -> None:
    async with ferrum.connect() as conn:
        await register_vector_codecs(conn)
        q = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]
        await Chunk.objects.create(conn, id=uuid4(), embedding=q)

        ordered = await Chunk.objects.nearest_to("embedding", q, metric="l2").limit(5).all(conn)
        scored = await vector_search(conn, Chunk, "embedding", q, metric="cosine", limit=5)
        print(len(ordered), scored[0]["score"])
```

Ensure migrations created the table + (optional) HNSW index and that `vector` is
installed before inserts.
