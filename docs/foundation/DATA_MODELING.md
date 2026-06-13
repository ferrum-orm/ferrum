# Ferrum Data Modeling

**Status:** Proposed — pending CEO/board approval
**Version:** v0.1 data model contract
**Inputs:** [ARCHITECTURE.md](./ARCHITECTURE.md), [PRODUCT_REQUIREMENTS.md](./PRODUCT_REQUIREMENTS.md), [ARCHITECTURE_FEASIBILITY_REVIEW.md](./ARCHITECTURE_FEASIBILITY_REVIEW.md), [SECURITY_REVIEW_PRD.md](./SECURITY_REVIEW_PRD.md)
**Issue:** GUY-70
**Blocked by:** GUY-69 (ARCHITECTURE.md — done)
**Date:** 2026-06-13

---

## 1. Purpose & Scope

This document specifies how Ferrum models data: the model base class and metaclass strategy, the field system, metadata representation, the constraint and index systems, validation hooks, and the relationship design. It is the authoritative data-model contract engineers implement against.

It is bound by the architecture invariants in [ARCHITECTURE.md §3](./ARCHITECTURE.md) and the product scope in [PRODUCT_REQUIREMENTS.md](./PRODUCT_REQUIREMENTS.md). Where the two disagree, the documents win and the conflict is flagged, not silently resolved.

### 1.1 Scope split (read this first)

The v0.1 product scope deliberately ships **scalar fields plus explicit foreign-key ID columns only** — no relationship loaders, no prefetch, no lazy traversal (PRD "Won't-have", Product Decisions §Relationship scope; ARCHITECTURE §10.1, §15). This document therefore separates two concerns the issue asked for:

| Concern | v0.1 | Design status |
|---------|------|---------------|
| Model class, field system, metadata, constraints, indexes, validation hooks | **Implemented** | Normative contract below |
| Relationship descriptors (`ForeignKey`, `ManyToMany`, `OneToOne`), lazy vs eager loading | **Not implemented** (YAGNI) | **Design seam only** — §7 documents the v0.2 contract so v0.1 metadata does not foreclose it |

Documenting the relationship *contract* now (without building loaders) satisfies Evolutionary Architecture: the v0.1 metadata shape must not paint v0.2 into a corner. Building the loaders now would violate the YAGNI invariant and the PRD scope. §7 is explicitly a forward contract, marked as such.

### 1.2 Lenses applied

- **Single Responsibility** — the model is the single source of truth; metadata is derived, never duplicated.
- **Least Astonishment** — field/constraint/index declaration mirrors Pydantic v2 + Django mental models.
- **Schema Evolution** — additive-by-default; metadata is introspectable so the migration planner ([MIGRATIONS.md](./MIGRATIONS.md), GUY-73) can diff it deterministically.
- **Defense in Depth** — every identifier a user can name (field, column, operator, sort) is an allowlist entry built at class-definition time; nothing reaches Rust as a runtime string.
- **Data Gravity** — type mapping and codecs are defined once, close to the wire format.
- **Evolutionary Architecture** — relationship and custom-codec seams are reserved without speculative machinery.

---

## 2. Model Class Design

### 2.1 Base class and inheritance

A Ferrum model **is** a Pydantic v2 model. `ferrum.Model` subclasses `pydantic.BaseModel` and layers persistence metadata derivation on top. There is no parallel persistence schema (PRD: Pydantic v2 Native Models).

```python
from ferrum import Model, Field
from datetime import datetime
from uuid import UUID


class User(Model):
    id: int = Field(primary_key=True)
    email: str = Field(max_length=320, unique=True)
    is_active: bool = True
    created_at: datetime = Field(default_factory=utcnow, db_index=True)

    class Meta:
        table = "users"
        indexes = [Index(fields=["email", "is_active"])]
        constraints = [Unique(fields=["email"])]
```

Key properties:

- The class body is **pure Pydantic v2** type annotations. `Field(...)` is a thin superset of `pydantic.Field` carrying persistence attributes (see §3).
- Validation, serialization, defaults, and optionality come from Pydantic v2 unchanged (PRD acceptance: "Defaults and nullability behave predictably").
- Persistence metadata (`ModelMetadata`) is derived **once** at class creation and is immutable thereafter (ARCHITECTURE §3 invariant 5, §8.1).

### 2.2 Metaclass strategy

Ferrum hooks model construction through Pydantic v2's `ModelMetaclass`. The chosen mechanism is a **metaclass subclass** (`FerrumModelMeta(ModelMetaclass)`), not `__init_subclass__` alone.

**Decision: subclass `ModelMetaclass`.**

| Alternative | Why rejected |
|-------------|--------------|
| `__init_subclass__` only | Runs *after* Pydantic has finalized `model_fields`; cannot cleanly intercept field-construction ordering or inject persistence field info before Pydantic validates the class. Workable but fragile across Pydantic minor versions (Least Astonishment risk). |
| Decorator (`@ferrum.model`) | Loses the `class User(Model)` ergonomic the README commits to; two ways to declare a model (Least Astonishment). |
| Runtime registry scan / import hooks | Hidden global side effects; ordering-dependent; hard to reason about (Blast Radius). |

**Metaclass responsibilities (definition-time only, no I/O):**

1. Let Pydantic build `model_fields` normally.
2. Walk `model_fields`, read Ferrum `Field` attributes from each field's `json_schema_extra`/metadata, and resolve `Meta`.
3. Derive `ModelMetadata`: table name, ordered column map, type mapping, PK, constraint set, index set, allowlists.
4. **Validate the persistence shape at class definition** — unsupported field types, conflicting PK declarations, duplicate column names, and unknown `Meta` keys raise at import time (PRD: "Unsupported fields raise actionable product-level errors"; ARCHITECTURE §8.1).
5. Freeze `ModelMetadata` (e.g. a frozen dataclass / `MappingProxyType` maps) and register it on the class as `Model.__ferrum_metadata__`.

**Concurrency note:** all of the above runs synchronously on the importing thread at class-definition time. After freeze, the metadata is read-only and shared safely across all `asyncio` tasks without locks (ARCHITECTURE §6.3). No per-request mutation ever touches it.

### 2.3 Manager / `objects` entry point

Each model exposes `objects` — a `Manager` that returns fresh `QuerySet` instances bound to the model's metadata. The manager holds **no mutable per-request state**; it is a factory. QuerySet semantics are owned by [QUERY_ENGINE.md](./QUERY_ENGINE.md) (GUY-72); this document only fixes that `objects` is the metadata-bound query entry point and that `ModelMetadata` is what the QuerySet carries into the Rust IR.

### 2.4 Reserved / abstract models

- A model may set `Meta.abstract = True` to act as a shared field mixin with **no table and no metadata registration** (Django-parity ergonomic). Abstract models are validated for field shape but produce no DDL.
- `id` is auto-provided as `BIGINT` primary key when the user declares no primary key (see §3.4). Explicit PK declarations override the default.

---

## 3. Field System

### 3.1 Type-driven derivation

Persistence column metadata is **derived from the Python type annotation**, not separately declared. `Field(...)` only adds persistence attributes the annotation cannot express (length, uniqueness, index, db column name, PK).

```python
class Product(Model):
    id: UUID = Field(primary_key=True, default_factory=uuid4)
    sku: str = Field(max_length=64, unique=True)
    price_cents: int
    description: str | None = None          # Optional[str] → nullable TEXT
    created_at: datetime
```

Derivation rules:

- Optionality: `T | None` / `Optional[T]` → nullable column. Non-optional → `NOT NULL`.
- Default: a Pydantic default or `default_factory` informs DDL `DEFAULT` where representable; otherwise the default is applied application-side at create time and documented as such (Least Astonishment — no surprising server defaults).
- Length: `Field(max_length=n)` on `str` → `VARCHAR(n)`; absent → `TEXT`.

### 3.2 Type mapping contract (v0.1 scalar subset)

This table is the **authoritative v0.1 type map** and the closed allowlist for supported field types. It refines [ARCHITECTURE §10.3](./ARCHITECTURE.md).

| Python / Pydantic type | PostgreSQL type | Nullability | Notes |
|------------------------|-----------------|-------------|-------|
| `int` | `BIGINT` (PK) / `INTEGER` (non-PK opt-in) | per Optional | PK defaults to `BIGINT`; `Field(int32=True)` opt-in `INTEGER` |
| `str` | `TEXT` or `VARCHAR(n)` | per Optional | `VARCHAR(n)` when `max_length` set |
| `bool` | `BOOLEAN` | per Optional | |
| `float` | `DOUBLE PRECISION` | per Optional | use `Decimal` for money, not `float` |
| `Decimal` | `NUMERIC(p, s)` | per Optional | `Field(max_digits=p, decimal_places=s)` required |
| `datetime` | `TIMESTAMPTZ` | per Optional | UTC-normalized; naive datetimes rejected at validation |
| `date` | `DATE` | per Optional | |
| `time` | `TIME` | per Optional | |
| `UUID` | `UUID` | per Optional | |
| `bytes` | `BYTEA` | per Optional | |
| `dict` / Pydantic submodel | `JSONB` | per Optional | stored as JSONB; not indexed by default |
| `Enum` (str/int) | `TEXT`/`INTEGER` + `CHECK` | per Optional | value set enforced via CHECK constraint (§5) |
| FK id (e.g. `author_id: int`) | matches referenced PK type | per Optional | **explicit ID column only in v0.1** (§7) |

**Unsupported types fail at class definition** with a structured error naming the field and type. There is no silent fallthrough (Defense in Depth; PRD acceptance).

### 3.3 `Field` attribute surface (v0.1)

`ferrum.Field` extends `pydantic.Field`. Persistence attributes:

| Attribute | Meaning | Affects |
|-----------|---------|---------|
| `primary_key: bool` | Marks the PK column | DDL, metadata |
| `unique: bool` | Single-column unique constraint | constraint set, DDL |
| `db_index: bool` | Single-column B-tree index | index set, DDL |
| `db_column: str` | Override column name (else field name) | column map |
| `max_length: int` | `VARCHAR(n)` for `str` | DDL |
| `max_digits` / `decimal_places` | `NUMERIC(p,s)` for `Decimal` | DDL |
| `int32: bool` | Use `INTEGER` instead of `BIGINT` | DDL |
| `server_default: str \| None` | Literal PostgreSQL default expression (allowlisted forms) | DDL |

All Pydantic-native arguments (`default`, `default_factory`, `description`, validators) pass through unchanged.

**Security:** `db_column`, `table`, and any user-named identifier become **metadata allowlist entries** validated at definition time. They are stored as resolved indices/enums in `ModelMetadata` and never re-interpreted as runtime strings when the Rust compiler emits SQL (ARCHITECTURE §5.4, §11.2 SQL-1). `server_default` accepts only an allowlisted set of safe expressions (e.g. `now()`, numeric/boolean/string literals); arbitrary SQL is rejected (no raw-SQL escape hatch, PRD SQL Compilation Safety).

### 3.4 Primary key rules

- Exactly one primary key per concrete model.
- No explicit PK → Ferrum injects `id: int = Field(primary_key=True)` mapped to `BIGINT GENERATED BY DEFAULT AS IDENTITY` (Least Astonishment; Django-parity auto-id).
- Composite primary keys are **out of v0.1 scope** (YAGNI); the metadata representation (§4) models PK as an ordered column list so a composite PK is an additive change later, not a redesign (Schema Evolution).

---

## 4. Metadata Representation

`ModelMetadata` is the immutable contract that crosses the PyO3 boundary alongside each `QuerySetIR` (ARCHITECTURE §5.4). It is the single artifact the Rust compiler, hydrator, and migration planner read.

### 4.1 Conceptual shape

```text
ModelMetadata (frozen, built once at class definition)
├── table_name: str                       # validated identifier
├── schema: str | None                    # PostgreSQL schema (default "public")
├── columns: ordered map<field_name, ColumnMeta>
│     └── ColumnMeta { column_name, pg_type, nullable, default,
│                      is_primary_key, codec_id }
├── primary_key: [field_name, ...]         # ordered (list to allow future composite)
├── constraints: [ConstraintMeta, ...]     # unique / check / composite (§5)
├── indexes: [IndexMeta, ...]              # single / composite / partial (§6)
├── allowlists:
│     ├── field_names: set<str>            # filter/order targets
│     ├── column_names: set<str>
│     └── operators: per-field supported operator set
└── metadata_version: int                  # bumped on contract-shape change
```

### 4.2 Properties

- **Immutable after build.** Frozen structures; any attempt to mutate is a programming error. This is what makes concurrent compilation lock-free (ARCHITECTURE §3 invariant 5/6, §6.3).
- **Introspectable.** Metadata is a first-class, readable Python object (`Model.__ferrum_metadata__`) and serializes to the typed IR payload. This is the acceptance criterion "constraints and indexes are declarative and introspectable" and the input the migration planner diffs.
- **Identifier resolution is structural.** Field/column/operator names resolve to enum indices in the IR; the Rust side never receives a raw user string in an identifier position (ARCHITECTURE §5.4; PRD SQL-1).
- **Versioned.** `metadata_version` mirrors the IR `version` field; an incompatible version fails fast at the boundary (Evolutionary Architecture, Schema Evolution).

### 4.3 What metadata does *not* carry

- No connection info, DSN, or credentials.
- No bound values or sample row data.
- No mutable per-request fields.

This keeps the metadata payload safe to log at Tier A and safe to share across tasks.

---

## 5. Constraint System

Constraints are declared on the model, captured in `ModelMetadata`, and emitted as PostgreSQL constraints by the migration planner. They are declarative and introspectable (acceptance criterion).

### 5.1 Supported constraint types (v0.1)

| Constraint | Declaration | PostgreSQL emission |
|------------|-------------|---------------------|
| Single-column unique | `Field(unique=True)` | `UNIQUE (col)` |
| Composite unique | `Meta.constraints = [Unique(fields=["a", "b"])]` | `UNIQUE (a, b)` |
| Check | `Meta.constraints = [Check(name=..., expr=...)]` | `CHECK (expr)` |
| Not null | derived from non-Optional type | `NOT NULL` |
| Enum value set | `Enum` field type | `CHECK (col IN (...))` |

```python
class Account(Model):
    id: int = Field(primary_key=True)
    owner_id: int
    currency: str = Field(max_length=3)
    balance_cents: int

    class Meta:
        constraints = [
            Unique(fields=["owner_id", "currency"], name="uq_account_owner_currency"),
            Check(name="ck_balance_nonneg", expr="balance_cents >= 0"),
        ]
```

### 5.2 Constraint expression safety

`Check.expr` and enum value sets are **not** raw user SQL strings injected at query time. They are:

- Captured at class-definition time into metadata (static, not per-request).
- Emitted only by the migration planner during DDL generation, never on the hot query path.
- Subject to the same no-raw-SQL-on-the-data-path invariant: a `CHECK` expression participates in DDL only; it is never a query escape hatch (PRD SQL Compilation Safety; ARCHITECTURE §11).

Engineers must constrain `Check.expr` to reference only declared columns of the model and an allowlisted operator/function set; an expression referencing unknown identifiers fails at definition time. (Detailed DDL emission and the destructive-change classification for constraints live in [MIGRATIONS.md](./MIGRATIONS.md), GUY-73.)

### 5.3 Foreign-key constraints (v0.1 nuance)

v0.1 supports an **explicit FK ID column** (e.g. `author_id: int`). Whether Ferrum emits a database-level `FOREIGN KEY ... REFERENCES` constraint for it is a declarative opt-in:

```python
class Post(Model):
    id: int = Field(primary_key=True)
    author_id: int = Field(references="users.id", on_delete="RESTRICT")
```

- `references` records the target table/column in metadata and lets the migration planner emit a real FK constraint (data integrity at the DB layer — Defense in Depth).
- This is **a column-level constraint, not a relationship loader.** There is no `post.author` traversal in v0.1. Naming the target is what §7's v0.2 relationship descriptors will build on.
- `on_delete` is an allowlisted enum (`RESTRICT`, `CASCADE`, `SET NULL`, `NO ACTION`); arbitrary values fail at definition.

---

## 6. Index Strategy

Indexes are declarative metadata, introspectable, and emitted by the migration planner. They never affect query compilation correctness — they are a performance contract.

### 6.1 Supported index types (v0.1)

| Index | Declaration | Emission |
|-------|-------------|----------|
| Single-column | `Field(db_index=True)` | `CREATE INDEX ... (col)` |
| Composite | `Meta.indexes = [Index(fields=["a", "b"])]` | `CREATE INDEX ... (a, b)` |
| Partial | `Index(fields=["a"], where="is_active")` | `CREATE INDEX ... (a) WHERE is_active` |
| Unique index | `Index(fields=["a"], unique=True)` | `CREATE UNIQUE INDEX ...` |

```python
class Session(Model):
    id: UUID = Field(primary_key=True, default_factory=uuid4)
    user_id: int = Field(references="users.id")
    token_hash: str = Field(unique=True)
    expires_at: datetime
    revoked: bool = False

    class Meta:
        indexes = [
            Index(fields=["user_id", "expires_at"]),                 # composite
            Index(fields=["expires_at"], where="revoked = false"),   # partial
        ]
```

### 6.2 Index naming & determinism

- Index names are either explicit (`Index(name=...)`) or **deterministically derived** from table + columns + predicate hash, so the same model produces the same index name across runs. Deterministic naming is required for the migration planner to diff stable index identity (Schema Evolution; PRD: "Migration results are deterministic for the same schema state").
- The partial-index `where` predicate is captured statically into metadata, references only declared columns, and is allowlist-validated at definition time (same rule as `Check.expr`, §5.2). It is DDL-only and not a query-path string.

### 6.3 Concurrency / operational note

`CREATE INDEX CONCURRENTLY` is non-transactional and is one of the documented per-step migration exceptions (ARCHITECTURE §ADR-004). Index *declaration* lives here; *how* it is applied safely (transactional wrapper exceptions, recovery) is owned by [MIGRATIONS.md](./MIGRATIONS.md). This doc only guarantees the index metadata is rich enough (columns, uniqueness, predicate) for the planner to choose the right emission strategy.

---

## 7. Relationship Design (v0.2 forward contract — not implemented in v0.1)

> **Scope marker.** Relationship descriptors and loading are **out of v0.1 scope** (PRD Won't-have; Product Decisions §Relationship scope; ARCHITECTURE §15.2). This section is a *design contract* so the v0.1 metadata and IR do not foreclose v0.2 relationships (Evolutionary Architecture). **Engineers must not implement relationship loaders, lazy traversal, or prefetch in v0.1.** Doing so violates the YAGNI invariant and the approved product scope.

### 7.1 What v0.1 actually ships

- Explicit FK **ID columns** (`author_id: int`) with optional DB-level FK constraints (§5.3).
- No `.author` attribute, no `.posts` reverse accessor, no `select_related`/`prefetch_related`.
- Joins across models are an **application responsibility** in v0.1 (issue two queries, or model the FK id directly). N+1 is explicitly the app's concern in v0.1 (ARCHITECTURE §14).

### 7.2 v0.2 relationship descriptor contract (design only)

When relationships land, they will be **descriptors layered on the existing FK metadata**, not a new persistence schema:

| Descriptor | Backing column(s) | Cardinality | Reverse accessor |
|------------|-------------------|-------------|------------------|
| `ForeignKey(Target, on_delete=...)` | local FK id column | many-to-one | one-to-many on `Target` |
| `OneToOne(Target, on_delete=...)` | local FK id column + unique constraint | one-to-one | one-to-one on `Target` |
| `ManyToMany(Target, through=...)` | join table (explicit or generated) | many-to-many | many-to-many on `Target` |

Design rules that v0.1 must preserve:

- A `ForeignKey` descriptor must be expressible as **exactly the v0.1 FK-id column plus a Python-side accessor**. The column metadata shape in §4/§5.3 is therefore already the storage contract; v0.2 only adds the descriptor and loader.
- `OneToOne` = `ForeignKey` + a unique constraint on the FK column — both already representable in v0.1 metadata.
- `ManyToMany` requires a **through model** (itself an ordinary Ferrum model). v0.1's model/metadata system can already express a join model with two FK-id columns and a composite unique constraint, so M2M is additive.

### 7.3 Lazy vs eager loading (v0.2 design)

Loading strategy must respect the async invariant: **traversal that hits the database is awaitable; attribute access is not implicitly blocking.**

| Strategy | API shape (v0.2) | Execution |
|----------|------------------|-----------|
| Explicit async fetch (default) | `author = await post.author.fetch()` | one awaited query; no hidden I/O |
| Eager join | `await Post.objects.select_related("author").all()` | single SQL with JOIN; hydrated together |
| Eager batched | `await Post.objects.prefetch_related("comments").all()` | second batched query keyed by FK; avoids N+1 |

**Hard async constraint (binds the v0.2 design now):** Ferrum will **not** offer implicit synchronous lazy loading (i.e. plain `post.author` silently issuing a blocking query). That pattern hides I/O and blocks the event loop — a direct violation of the async-first invariant (ARCHITECTURE §3 invariant 3; PRD Async-first). Any relationship traversal that touches the database is an explicit `await`. This rule is stated here, in the data model, so that v0.1 FK accessors are never quietly given a sync-loading shortcut.

Loader correctness, batching, and the join-vs-batch compilation belong to [QUERY_ENGINE.md](./QUERY_ENGINE.md) when relationships are scheduled; this section only fixes the data-model-level contract and the async guarantee.

---

## 8. Validation Hooks

Ferrum distinguishes **input validation** (untrusted, full Pydantic v2) from **hydration** (trusted DB-origin rows, fast path). This split is an architecture decision (ADR-003) with direct data-model consequences.

### 8.1 Field-level and model-level validation (input path)

On the **write path** (`create`, `update` with submitted data), input is untrusted and runs full Pydantic v2 validation (ARCHITECTURE §9.2):

| Hook | Mechanism | Runs when |
|------|-----------|-----------|
| Field-level validator | `@field_validator("email")` (Pydantic v2) | on input validation, before IR build |
| Model-level validator | `@model_validator(mode="after")` | on input validation, before IR build |
| Pre-save hook | Ferrum `async def before_save(self)` | after validation, before SQL execute |

```python
class User(Model):
    id: int = Field(primary_key=True)
    email: str

    @field_validator("email")
    @classmethod
    def normalize_email(cls, v: str) -> str:
        return v.strip().lower()

    async def before_save(self) -> None:
        # async pre-save hook: runs in the event loop, may await
        self.email_verified = await verification_service.is_known(self.email)
```

Rules:

- Validation failures reference the **offending field** and constraint, and **never echo the submitted value** by default (PRD Error Handling; SECURITY_REVIEW_PRD). The error mapper (ARCHITECTURE §8.8) enforces this; model authors must not log raw input in validators.
- `before_save` is **async** (`async def`) — it runs in the host event loop and may `await`. This honors the async-first invariant: no blocking pre-save callback on the I/O path.
- Pre-save runs in Python *before* the IR is built, so a hook can still reject or transform the instance before any SQL exists (Defense in Depth).

### 8.2 Hydration path (read path) — ADR-003

On the **read path**, rows originate from PostgreSQL, which already enforced types. Ferrum hydrates via Pydantic v2 **construct-without-revalidate** by default (ARCHITECTURE §8.5, ADR-003):

- `@field_validator` / `@model_validator` with **side effects are NOT re-run** on DB-origin data by default. This is a documented caveat (ADR-003) and a Least-Astonishment hazard model authors must understand: a validator that mutates or calls out will not fire on hydration.
- Models that genuinely need full validation on read can opt into a revalidating hydration mode (per-query or per-model flag). This is the documented escape valve, not the default (performance: Data Gravity / Doherty threshold).

### 8.3 Hook execution ordering (write path)

```text
submitted data
  │
  ├─ Pydantic field_validator   (sync, per field)
  ├─ Pydantic model_validator   (sync, whole model)
  ├─ Ferrum before_save         (async, may await)
  ├─ build QuerySetIR           (Python, no I/O)
  ├─ Rust compile → SQL+params  (sync, GIL-held)
  ├─ asyncpg execute            (async, cancellable)
  ├─ Rust hydrate returned row  (sync)
  └─ Ferrum after_save(self)    (async, optional, may await)
```

`after_save` is an optional async post-commit-of-statement hook for symmetry; like `before_save` it runs in the event loop and is never given a blocking variant. Both are dispatched by the Python layer, not Rust.

### 8.4 Validation vs constraints (where each lives)

| Concern | Enforced by | When |
|---------|-------------|------|
| Field type / format / business rule on input | Pydantic validators (§8.1) | before SQL |
| Uniqueness / check / FK integrity | PostgreSQL constraints (§5) | at execute (mapped to sanitized `FerrumIntegrityError`) |
| Enum membership | both: validator (input) + `CHECK` (storage) | layered (Defense in Depth) |

Constraint violations surfacing from PostgreSQL are mapped through the centralized error boundary to a sanitized `FerrumIntegrityError` carrying field/constraint **names but not row values** (ARCHITECTURE §6.4, §8.8; PRD Error Handling).

---

## 9. End-to-End Example (v0.1, scalar + FK-id)

```python
from ferrum import Model, Field, Index, Unique, Check
from datetime import datetime
from uuid import UUID, uuid4


class User(Model):
    id: int = Field(primary_key=True)
    email: str = Field(max_length=320, unique=True)
    is_active: bool = True
    created_at: datetime = Field(default_factory=utcnow, db_index=True)

    class Meta:
        table = "users"


class Post(Model):
    id: UUID = Field(primary_key=True, default_factory=uuid4)
    author_id: int = Field(references="users.id", on_delete="RESTRICT")  # explicit FK id
    title: str = Field(max_length=200)
    body: str
    published: bool = False
    created_at: datetime = Field(default_factory=utcnow)

    class Meta:
        table = "posts"
        indexes = [
            Index(fields=["author_id", "created_at"]),
            Index(fields=["created_at"], where="published = true"),
        ]
        constraints = [Check(name="ck_title_nonempty", expr="char_length(title) > 0")]


# Create (write path: full Pydantic validation + before_save)
post = await Post.objects.create(author_id=user.id, title="Hello", body="...")

# Query (read path: construct-without-revalidate hydration)
recent = await (
    Post.objects
    .filter(author_id=user.id, published=True)
    .order_by("-created_at")
    .limit(10)
    .all()
)

# v0.1 relationship: explicit FK id, NO post.author traversal
author = await User.objects.get(id=post.author_id)
```

Everything above is expressible with the v0.1 field/metadata/constraint/index system. The only "relationship" is the explicit `author_id` column and an optional DB FK constraint — consistent with PRD scope.

---

## 10. Security Requirements for Engineers (data-model surface)

These map PRD/ARCHITECTURE security gates to the data-model layer. They are release-qualification gates and must be test-covered.

| ID | Requirement | Enforcement point | Test |
|----|-------------|-------------------|------|
| DM-1 | Every user-named identifier (field, `db_column`, `table`, `schema`) becomes a metadata allowlist entry at class definition | Metaclass (§2.2) | Unknown field in filter → structured error, no SQL |
| DM-2 | `Check.expr` / partial-index `where` / `server_default` accept only allowlisted forms referencing declared columns | Metadata builder (§5.2, §6.2) | Arbitrary SQL fragment → definition-time error |
| DM-3 | Unsupported field type fails at class definition | Type map (§3.2) | Unmapped type → actionable error naming field/type |
| DM-4 | Validation errors name field/constraint, never echo submitted value | Error boundary + validators (§8.1) | Fixture invalid input → error has no submitted value substring |
| DM-5 | Hydration default is construct-without-revalidate; revalidation is explicit opt-in | Hydration path (§8.2, ADR-003) | Side-effect validator not run on read by default |
| DM-6 | `on_delete` / FK `references` are allowlisted enums/identifiers | FK metadata (§5.3) | Bad `on_delete` value → definition-time error |
| DM-7 | Metadata payload carries no DSN/credential/bound value/row data | `ModelMetadata` (§4.3) | Metadata serialization scan for secrets |

**SecurityEngineer notification:** the field-identifier allowlist derivation (DM-1), the `Check`/partial-index/`server_default` expression allowlisting (DM-2), and the FK constraint emission path require SecurityEngineer review at v0.1 release qualification, since they define what user-named strings reach DDL/SQL. This complements the SQL-compilation review already flagged in [ARCHITECTURE §11.2](./ARCHITECTURE.md).

---

## 11. Alternatives Considered

| Decision | Chosen | Alternative | Why rejected |
|----------|--------|-------------|--------------|
| Model base | Subclass `pydantic.BaseModel` | Separate persistence schema mapped to a Pydantic DTO | Duplicate schema; violates Pydantic-native single-source-of-truth (PRD) |
| Class interception | `ModelMetaclass` subclass | `__init_subclass__` only / decorator | Cleaner field interception, keeps `class X(Model)` ergonomic, less Pydantic-version-fragile |
| Metadata mutability | Frozen, built once | Lazy/mutable metadata cache | Immutable enables lock-free concurrent compile (ARCHITECTURE §3, §6.3) |
| Identifiers across boundary | Resolved enums/indices in IR | Pass field-name strings to Rust | Structural injection-safety vs convention (Defense in Depth) |
| Relationships in v0.1 | Explicit FK id + forward contract | Build `ForeignKey`/loaders now | YAGNI + PRD scope; loaders are v0.2 |
| Lazy loading model | Explicit `await` traversal (v0.2) | Implicit sync lazy load | Hidden blocking I/O breaks async-first invariant |
| Composite PK | Modeled as ordered list, deferred | Build composite PK in v0.1 | YAGNI; ordered-list shape keeps it additive |

---

## 12. Open Items & Handoff

| Item | Owner | Blocks |
|------|-------|--------|
| Formal ADR records (incl. ADR-003 hydration) in `DECISIONS.md` | ChiefArchitect ([GUY-74](/GUY/issues/GUY-74)) | ADR sign-off |
| Constraint/index DDL emission + destructive classification detail | [MIGRATIONS.md](./MIGRATIONS.md) ([GUY-73](/GUY/issues/GUY-73)) | migration impl |
| QuerySet IR field/operator binding to this metadata | [QUERY_ENGINE.md](./QUERY_ENGINE.md) ([GUY-72](/GUY/issues/GUY-72)) | query impl |
| SecurityEngineer review of identifier/expression allowlisting (DM-1, DM-2) | SecurityEngineer | release qualification |
| CEO/board approval of data-model contract | CEO | model implementation start |

### Acceptance criteria coverage (GUY-70)

- **Model definition is entirely Python-native with no schema duplication** — §2 (Pydantic v2 base, derived metadata).
- **Relationships support async traversal** — §7.3 (explicit `await` traversal contract; no implicit sync load). v0.1 ships explicit FK ids per PRD scope; async-traversal rule fixed for v0.2.
- **Constraints and indexes are declarative and introspectable** — §4 (introspectable `ModelMetadata`), §5, §6.
- **Field system is type-safe with Pydantic v2** — §3 (type-driven derivation, closed type map, definition-time rejection).
- **Document committed to `/docs/foundation/DATA_MODELING.md`** — this file.

### Engineer handoff

Implementation of the data-model layer may begin once this document and `DECISIONS.md` are approved. Recommended slice order aligns with [ARCHITECTURE §17](./ARCHITECTURE.md): build `ModelMetadata` + metaclass + field derivation first (it is the input to both the Rust IR and the migration planner), with the §10 security gates test-covered from the first slice.

**Do not implement (architecture/scope guard):** relationship descriptors, lazy/prefetch loaders, composite primary keys, custom field codecs, or any implicit synchronous database access. These are deferred seams (§7, §3.4), not v0.1 work.

---

*Produced by Chief Architect for GUY-70. Pending CEO/board data-model approval.*
