# Indexes Guide

How Ferrum declares, plans, and emits indexes — single-column, composite, partial,
access-method-specific, and full-text — across supported drivers.

> **Security:** index names, columns, and access methods come from model-metadata
> allowlists. Access methods outside `{btree, gin, gist, hash, brin, hnsw, ivfflat}`
> fail at plan/SQL emission with `[FERR-M001]` before any DDL runs. Partial-index
> `where` clauses are authored by you in migrations/models (trusted project code),
> not taken from request input.

---

## 1. Ways to declare an index

| Mechanism                                       | When to use                       | Migration op                               |
| ----------------------------------------------- | --------------------------------- | ------------------------------------------ |
| `Field(db_index=True)`                          | Single-column btree               | `AddIndex` (auto name)                     |
| `Field(unique=True)`                            | Unique constraint / unique index  | Unique column + optional `AddIndex`        |
| `Meta.indexes = [Index(...)]`                   | Composite, partial, GIN/HNSW/etc. | `AddIndex`                                 |
| `Meta.full_text_indexes = [FullTextIndex(...)]` | Cross-dialect FTS                 | `CreateFullTextIndex` (+ catalog on MSSQL) |

`compute_plan` / `makemigrations` emit index ops **after** `create_table` (and after
new columns on alter paths).

---

## 2. Field-level indexes

```python
from typing import Annotated
import ferrum
from ferrum import Field, Model


class User(Model):
    id: int
    email: Annotated[str, Field(unique=True, db_index=True)]
    display_name: Annotated[str, Field(db_index=True)]
```

- `db_index=True` → planner emits `AddIndex` on that column after table create.
- `unique=True` → unique column constraint in DDL; may also pair with an index depending
  on dialect defaults.

Prefer `Meta.indexes` when you need a custom name, composite keys, `USING`, or `WHERE`.

---

## 3. Declarative `Meta.indexes`

```python
from typing import ClassVar
import ferrum
from ferrum import Index, Model


class Post(Model):
    id: int
    author_id: int
    body: str
    published: bool
    published_at: str | None = None

    class Meta:
        indexes: ClassVar[list[Index]] = [
            # Composite btree (default using="btree")
            Index(fields=("author_id", "published_at")),
            # Explicit name + unique
            Index(fields=("author_id", "id"), name="idx_post_author_id_pk", unique=True),
            # Partial GIN on text (PostgreSQL: needs pg_trgm → gin_trgm_ops)
            Index(fields=("body",), using="gin", where="published = true"),
        ]
```

### `Index` fields

| Field    | Type              | Default   | Notes                                                                                   |
| -------- | ----------------- | --------- | --------------------------------------------------------------------------------------- |
| `fields` | `tuple[str, ...]` | required  | Model field names (allowlisted at class definition). Unknown fields raise `ValueError`. |
| `name`   | `str \| None`     | auto      | Auto: `idx_{table}_{field1}_{field2}_…`                                                 |
| `unique` | `bool`            | `False`   | Emits `CREATE UNIQUE INDEX`                                                             |
| `using`  | `str`             | `"btree"` | Must be in the access-method allowlist                                                  |
| `where`  | `str \| None`     | `None`    | Partial index predicate (PostgreSQL-style `WHERE`)                                      |

### Access methods

Allowlisted values (case-sensitive as stored):

```
btree | gin | gist | hash | brin | hnsw | ivfflat
```

| Method    | Typical use                                         |
| --------- | --------------------------------------------------- |
| `btree`   | Equality / range / sort keys (default)              |
| `hash`    | Equality-only lookups                               |
| `gin`     | Arrays, JSONB, `tsvector`, trigram text (`pg_trgm`) |
| `gist`    | Geometric / exclusion / some FTS patterns           |
| `brin`    | Large append-only / time-series columns             |
| `hnsw`    | pgvector approximate nearest neighbor               |
| `ivfflat` | pgvector IVF ANN                                    |

---

## 4. Operator classes (PostgreSQL)

When `using="gin"` and a column is plain `text`, Ferrum auto-attaches
`gin_trgm_ops` so PostgreSQL can build the index (requires the `pg_trgm`
extension). `tsvector` columns use the default GIN opclass — no suffix.

```python
# TEXT + GIN → opclasses=["gin_trgm_ops"] in the plan
Index(fields=("body",), using="gin")

# TSVECTOR + GIN → no opclass
Index(fields=("search_vector",), using="gin")
```

For pgvector HNSW/IVFFlat you usually set opclasses **manually** in a migration
`AddIndex` (makemigrations does not invent `vector_*_ops` today):

```python
from ferrum import migrations as ops

ops.AddIndex(
    "tickets",
    "idx_tickets_emb_hnsw",
    ["summary_embedding"],
    using="hnsw",
    opclasses=["vector_cosine_ops"],
)
```

Common pgvector opclasses: `vector_cosine_ops`, `vector_l2_ops`, `vector_ip_ops`.

---

## 5. Full-text indexes (`FullTextIndex`)

For dialect-native FTS on **base text columns** (not a stored `TSVector`), declare:

```python
from typing import ClassVar
from ferrum import FullTextIndex, Model


class Article(Model):
    id: int
    title: str
    body: str

    class Meta:
        full_text_indexes: ClassVar[list[FullTextIndex]] = [
            FullTextIndex(fields=("title", "body"), name="ft_article_title_body", config="english"),
        ]
```

| Field                  | Purpose                                                |
| ---------------------- | ------------------------------------------------------ |
| `fields`               | Base-table columns to index                            |
| `name`                 | Index / virtual-table name (auto when omitted)         |
| `config`               | PostgreSQL `regconfig` / language hint                 |
| `sqlite_content_table` | SQLite FTS5 external-content source when ≠ model table |

On PostgreSQL, prefer a `TSVector` column + `Index(..., using="gin")` for stored
vectors; use `FullTextIndex` when you want expression GIN over `to_tsvector(...)`
without a dedicated column. See [Vector Guide](./vector.md) for KNN; FTS query APIs
(`search`, `rank_by`, `__match*`) are covered in [Getting Started](./getting-started.md)
and [API Reference](./api-reference.md).

---

## 6. Migration ops

```python
from ferrum import migrations as ops

ops.AddIndex("posts", "idx_posts_author", ["author_id"], using="btree")
ops.AddIndex(
    "posts",
    "idx_posts_body_gin",
    ["body"],
    using="gin",
    where="published = true",
    opclasses=["gin_trgm_ops"],
)
ops.DropIndex("idx_posts_author")  # PostgreSQL; some dialects need table in plan dict

ops.CreateFullTextIndex("articles", "ft_articles", ["title", "body"], config="english")
ops.DropFullTextIndex("articles", "ft_articles")
ops.CreateFullTextCatalog("main_catalog")  # MSSQL only
```

`AddIndex` is classified **safe**. `DropIndex` is also **safe** in Ferrum's taxonomy
(still irreversible for query plans — review carefully).

Offline preview:

```bash
ferrum sqlmigrate 0002_add_indexes --dialect postgres
```

---

## 7. Per-driver behavior

### PostgreSQL (`ferrum-orm[pg]`, asyncpg)

- Emits `CREATE [UNIQUE] INDEX IF NOT EXISTS … ON "table" USING <method> (cols)`.
- Honors `using`, `where`, and `opclasses`.
- `DROP INDEX IF EXISTS "name"`.
- GIN + text → `gin_trgm_ops` (install `CREATE EXTENSION IF NOT EXISTS pg_trgm`).
- HNSW / IVFFlat require `CREATE EXTENSION vector` (see [Vector Guide](./vector.md)).
- FTS expression indexes via `CreateFullTextIndex` →
  `CREATE INDEX … USING gin (to_tsvector('config', …))`.

**Concurrency / failure modes:** large indexes can lock writes for a long time.
Ferrum's `AddIndex` path is **transactional** (`CREATE INDEX`, not
`CREATE INDEX CONCURRENTLY`). For huge production tables, plan maintenance windows
or author a careful non-transactional workflow outside the default migrate path.
Invalid `using` / types fail before apply with `[FERR-M001]`.

### MySQL (`ferrum-orm[mysql]`, asyncmy)

- Emits `CREATE [UNIQUE] INDEX IF NOT EXISTS … ON \`table\` (cols)`— **no`USING`\*\*
  clause in the current emitter (access method is effectively InnoDB btree).
- `DROP INDEX \`name\` ON \`table\``.
- Full-text: `FullTextIndex` → `ALTER TABLE … ADD FULLTEXT INDEX …`.
- GIN/HNSW/`opclasses` / partial `WHERE` are PostgreSQL-oriented; do not rely on them
  for MySQL thin parity.

### SQLite (`ferrum-orm[sqlite]`, aiosqlite)

- Similar btree `CREATE INDEX IF NOT EXISTS` without PostgreSQL `USING` semantics.
- Full-text: FTS5 **virtual table** + sync triggers via `CreateFullTextIndex`
  (`sqlite_content_table` when the content table name differs).
- Vector / HNSW / GIN opclasses are not applicable.

### SQL Server (`ferrum-orm[mssql]`, aioodbc) — thin parity

- Emits `CREATE [UNIQUE] INDEX [name] ON [table] (cols)` — **no** `IF NOT EXISTS`,
  **no** `USING`. Idempotency relies on the migration ledger (each migration runs once).
- `DROP INDEX [name] ON [table]`.
- `CreateFullTextCatalog` then `CreateFullTextIndex` for FTS
  (`WITH CHANGE_TRACKING AUTO`). Population is **asynchronous** — queries may miss
  rows until the catalog catches up (integration tests often need retry/backoff).
- Types like `VECTOR(...)` are **rejected** at DDL mapping (`[FERR-M001]`).
- PostgreSQL-only ops (`create_extension`, RLS, `alter_column`, …) are rejected on
  this backend.

---

## 8. Naming and drift

- Prefer stable explicit `name=` for production indexes you may reference in `DropIndex`.
- Editing an applied migration file fails checksum verification (`[FERR-M005]`).
- `ferrum migrate` may warn on schema drift when models are loaded via bootstrap;
  drift detection is best-effort and does not replace `makemigrations`.

---

## 9. Checklist

- [ ] Fields in `Index.fields` / `FullTextIndex.fields` exist on the model.
- [ ] `using` is allowlisted; vector ANN uses `hnsw`/`ivfflat` **and** the `vector` extension.
- [ ] GIN on text: ensure `pg_trgm` is installed (PostgreSQL).
- [ ] Partial `where` predicates match real column names/types.
- [ ] MSSQL FTS: catalog created before the full-text index; expect async population lag.
- [ ] Preview with `ferrum sqlmigrate … --dialect <driver>` before applying.
