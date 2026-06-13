# Ferrum Query Engine

**Status:** Proposed — pending CEO/board approval
**Version:** v0.1 query-engine contract
**Inputs:** [ARCHITECTURE.md](./ARCHITECTURE.md), [DATA_MODELING.md](./DATA_MODELING.md), [PRODUCT_REQUIREMENTS.md](./PRODUCT_REQUIREMENTS.md), [ARCHITECTURE_FEASIBILITY_REVIEW.md](./ARCHITECTURE_FEASIBILITY_REVIEW.md), [SECURITY_REVIEW_PRD.md](./SECURITY_REVIEW_PRD.md)
**Issue:** GUY-72
**Blocked by:** GUY-70 (DATA_MODELING.md — done)
**Date:** 2026-06-13

---

## 1. Purpose & Scope

This document specifies Ferrum's query engine: the `QuerySet` API surface and lazy-evaluation semantics, the filter/lookup system, the SQL generation strategy, the Python→Rust compilation pipeline, the async execution path over `asyncpg`, and the optimization seams reserved for later. It is the authoritative contract engineers implement the query path against.

It is bound by the architecture invariants in [ARCHITECTURE.md §3](./ARCHITECTURE.md) and consumes the metadata contract fixed by [DATA_MODELING.md §4](./DATA_MODELING.md). Where this document and those disagree, those documents win and the conflict is flagged, not silently resolved. This document **owns** the QuerySet/IR/compile/execute contract that [DATA_MODELING.md §2.3, §12](./DATA_MODELING.md) explicitly deferred to it.

### 1.1 Scope split (read this first)

| Concern | v0.1 | Design status |
|---------|------|---------------|
| Lazy `QuerySet`, chaining, terminal coroutines | **Implemented** | Normative contract (§3) |
| Filter lookups (`__eq`, `__contains`, `__in`, comparisons, null/bool) | **Implemented** | Normative contract (§4) |
| `QuerySetIR` → Rust → parameterized SQL pipeline | **Implemented** | Normative contract (§5–§6) |
| Async execution over asyncpg | **Implemented** | Normative contract (§7) |
| Relationship traversal, `select_related`, `prefetch_related` | **Not implemented** (YAGNI / PRD Won't-have) | **Design seam only** (§8) — IR must not foreclose it |
| Query batching, `EXPLAIN` integration | **Not implemented** | **Hooks identified** (§8), not built in v0.1 |

Documenting the optimization and relationship *contracts* now (without building loaders) satisfies Evolutionary Architecture: the v0.1 IR shape must not paint v0.2 into a corner. Building loaders/prefetch now would violate the YAGNI invariant and the approved PRD scope ([PRD Won't-have](./PRODUCT_REQUIREMENTS.md)).

### 1.2 Lenses applied

- **Single Responsibility** — Python owns the lazy builder and async I/O; Rust owns pure compilation. Neither leaks into the other.
- **Least Astonishment** — filter lookups and chaining mirror Django's QuerySet so a migrating team's muscle memory carries over (PRD persona).
- **Defense in Depth** — every field, operator, and sort direction is resolved against a metadata allowlist before SQL exists; values are bound parameters only.
- **Evolutionary Architecture** — the IR is typed and versioned so relationships and optimizers are additive, not a redesign.
- **Observability First** — every terminal op emits Tier A hook events with a query fingerprint; no bound values by default.
- **Data Gravity** — hydration decodes rows in Rust close to the wire format and constructs Pydantic instances via the construct-without-revalidate fast path (ADR-003).
- **Blast Radius** — cancellation/timeout live at the single Python await point; a compile failure produces no SQL and touches no connection.

---

## 2. Component Context

The query engine spans the three Ferrum packages defined in [ARCHITECTURE.md §7](./ARCHITECTURE.md). State ownership and the GIL/async boundary are fixed there; this section situates the query-path components.

```text
┌────────────────────────────────────────────────────────────────────────┐
│ ferrum-py (Python)                                                       │
│                                                                          │
│  Model.objects ─► Manager ─► QuerySet (lazy, immutable, chainable)       │
│        │                          │ .filter/.exclude/.order_by/.limit    │
│        │                          ▼ (no I/O)                             │
│        │                     QuerySetIR builder                          │
│        │                          │ typed IR + ModelMetadata ref         │
│        │                          ▼                                       │
│        │             ┌────────────────────────────┐                      │
│        │  terminal   │ await qs.all()/get()/count │  ◄── coroutine       │
│        │  op starts ─►│ /create/update/delete      │                      │
│        │             └─────────────┬──────────────┘                      │
│        │                           │ 1. compile (sync, GIL)              │
│        │   hooks (Tier A) ◄────────┤ 2. hook query_start                 │
│        │                           │ 3. pool.fetch (async, cancellable)  │
│        │   error boundary ◄────────┤ 4. hydrate (sync, GIL)              │
│        │                           │ 5. construct Pydantic + hook        │
└────────┼───────────────────────────┼─────────────────────────────────────┘
         │                           │ PyO3 (sync, GIL-held)
┌────────▼───────────────────────────▼─────────────────────────────────────┐
│ ferrum-pyo3  →  ferrum-core (Rust, pure sync)                            │
│   IR validator (allowlists) · SQL compiler · bound-param encoder ·       │
│   row decoder / hydration payload · structured CompileError              │
└──────────────────────────────────────────────────────────────────────────┘
```

| Component | Package | Responsibility |
|-----------|---------|----------------|
| `Manager` (`objects`) | ferrum-py | Metadata-bound factory for fresh `QuerySet`s; holds no per-request state ([DATA_MODELING.md §2.3](./DATA_MODELING.md)) |
| `QuerySet` | ferrum-py | Lazy, immutable, chainable builder; terminal ops are coroutines ([ARCHITECTURE.md §8.2](./ARCHITECTURE.md)) |
| `QuerySetIR` builder | ferrum-py | Translates the chain into the typed, versioned IR (ADR-002) |
| IR validator + SQL compiler | ferrum-core | Allowlist validation, parameterized SQL emission, fingerprint ([ARCHITECTURE.md §8.3](./ARCHITECTURE.md)) |
| Connection executor | ferrum-py | asyncpg pool acquire/execute; cancellation/timeout ([ARCHITECTURE.md §8.4](./ARCHITECTURE.md)) |
| Hydrator | ferrum-core + ferrum-py | Row decode (Rust) → construct Pydantic (Python), ADR-003 |

---

## 3. QuerySet Design

### 3.1 Laziness and the build/execute split

A `QuerySet` is a **lazy, immutable description of a query**. Chaining methods return a *new* `QuerySet` and perform **no I/O**. The database is contacted only when an `await`-ed terminal operation runs. This is the central Least-Astonishment and concurrency-safety property and directly satisfies the PRD acceptance criterion "Chain methods … are composable without hitting the database" ([PRD §Async QuerySet and CRUD](./PRODUCT_REQUIREMENTS.md)).

```python
qs = User.objects.filter(is_active=True)      # sync, lazy — no I/O, no SQL
qs = qs.exclude(role="banned")                # returns a NEW QuerySet
qs = qs.order_by("-created_at").limit(10)      # still lazy
users = await qs.all()                          # ← terminal: compile + execute here
```

**Immutability rule.** Each builder method copies the accumulated IR fragment and returns a new `QuerySet`; the source `QuerySet` is unchanged. This makes a `QuerySet` safe to share and re-use as a base for divergent queries, and it makes concurrent terminal ops on sibling QuerySets non-interfering ([ARCHITECTURE.md §6.3](./ARCHITECTURE.md): "Independent `await qs.all()` calls … do not mutate each other's compiled state"). A `QuerySet` is **single-shot per terminal call** — re-awaiting issues a fresh compile+execute (no implicit result cache in v0.1; caching is a §8 seam).

### 3.2 Chainable (lazy) methods — v0.1 surface

All methods below are synchronous, return a new `QuerySet`, and never touch the DB. They mirror Django's QuerySet to satisfy the migrating-team persona (PRD).

| Method | Semantics | IR effect |
|--------|-----------|-----------|
| `filter(**lookups)` | Conjunctive (`AND`) predicate; lookups per §4 | append predicate node(s) |
| `exclude(**lookups)` | Negated conjunctive predicate (`NOT (…)`) | append negated predicate group |
| `order_by(*fields)` | Sort spec; `"-field"` = DESC, `"field"` = ASC | set/replace ordered sort list |
| `limit(n)` | Row cap (`LIMIT $n` bound param) | set limit |
| `offset(n)` | Skip (`OFFSET $n` bound param) | set offset |
| `values(*fields)` | Project a subset of columns; result type is dict-like rows | set projection |
| `only(*fields)` / `defer(*fields)` | Column subset / column exclusion on model rows | set projection mask |

Notes:
- `filter`/`exclude` are **composable and order-independent for correctness** (all become `AND`-combined predicate groups; `exclude` wraps its group in `NOT`). Multiple `order_by` calls replace (Django semantics: last `order_by` wins) — documented to avoid surprise.
- `limit`/`offset` values are **bound parameters**, not interpolated literals (SQL-2).
- `values()`/`only()`/`defer()` change the projection and therefore the result type (§3.4); they remain pure IR mutations.

**Boolean logic beyond `AND` (Q-objects).** The PRD requires "boolean logic" and "common filter operators (… boolean logic)" ([PRD §62, §73](./PRODUCT_REQUIREMENTS.md)). v0.1 ships:
- Implicit `AND` across kwargs in one `filter`.
- `exclude` for negation.
- A `Q` object for explicit `AND`/`OR`/`NOT` composition: `filter(Q(a=1) | Q(b=2))`. `Q` builds the same predicate-node IR (a small boolean tree), so `OR` is structural in the IR, never a string fragment. This is the minimum surface that matches Django's mental model without speculative complexity (YAGNI for anything beyond `&`, `|`, `~`).

### 3.3 Terminal operations (coroutines)

Terminal operations are `async def` and are the **only** place I/O happens ([ARCHITECTURE.md §8.2, §6.1](./ARCHITECTURE.md)). Each one drives the full compile→execute→hydrate→hook pipeline (§5, §7).

| Terminal | Returns | SQL shape |
|----------|---------|-----------|
| `await qs.all()` | `list[Model]` (or list of dict rows for `values()`) | `SELECT … WHERE … ORDER BY … LIMIT … OFFSET …` |
| `await qs.get(**lookups)` | single `Model`; raises `DoesNotExist` / `MultipleObjectsReturned` | `SELECT … LIMIT 2` (detect multiplicity), exactly-one enforced in Python |
| `await qs.first()` | `Model \| None` | `SELECT … ORDER BY … LIMIT 1` |
| `await qs.count()` | `int` | `SELECT count(*) … WHERE …` |
| `await qs.exists()` | `bool` | `SELECT EXISTS(SELECT 1 … WHERE …)` |
| `await Model.objects.create(**data)` | created `Model` | `INSERT … RETURNING …` ([ARCHITECTURE.md §9.2](./ARCHITECTURE.md)) |
| `await qs.update(**data)` | affected row count | `UPDATE … SET … WHERE …` |
| `await qs.delete()` | deleted row count | `DELETE … WHERE …` |
| `async for row in qs` | async iterator over `Model` | streamed `fetch` (cursor seam, §8) |

**Result-type contract:**
- Default rows hydrate to the model class via construct-without-revalidate (ADR-003); a side-effecting validator does **not** re-run on read by default ([DATA_MODELING.md §8.2](./DATA_MODELING.md)).
- `values(*fields)` yields dict-like rows (no model construction) — useful for projections and aggregates.
- `get()` enforces exactly-one in Python after a `LIMIT 2` fetch, so multiplicity is detected without a second round trip and without trusting client input.

### 3.4 Danger API for unscoped mutations (Defense in Depth)

`update()` and `delete()` **without a filter that scopes the rows** must fail before any IR is emitted, per [ARCHITECTURE.md §8.2, §11.2 MIG-5](./ARCHITECTURE.md) and [PRD §215](./PRODUCT_REQUIREMENTS.md). The named danger API is the only way to mutate all rows:

```python
await User.objects.delete()                 # ← raises FerrumUnscopedMutationError (no SQL)
await User.objects.filter(is_active=False).delete()   # ok: scoped
await User.objects.danger_delete_all()      # explicit, audited all-rows delete
await User.objects.danger_update_all(is_active=False) # explicit, audited all-rows update
```

The guard runs in the Python QuerySet layer **before IR build** (fail-fast, Layer 1 of [ARCHITECTURE.md §11.1](./ARCHITECTURE.md)). It is a security gate (§9, QE-6), not an ergonomic nicety.

### 3.5 What QuerySet does NOT do in v0.1

- No relationship traversal (`post.author`), `select_related`, or `prefetch_related` ([DATA_MODELING.md §7](./DATA_MODELING.md)). Joins are an application responsibility (issue two queries or model the FK id).
- No raw SQL escape hatch (`extra()`, string fragments, user templates) — explicit non-goal (SQL-3, [PRD §122](./PRODUCT_REQUIREMENTS.md)).
- No implicit result caching across re-awaits (a §8 optimization seam).
- No aggregation framework beyond `count()`/`exists()` (group-by/annotate is a documented §8 seam; YAGNI for v0.1).

---

## 4. Filter System (Field Lookups)

### 4.1 Lookup syntax

Lookups use Django's `field__operator=value` keyword syntax. A bare `field=value` is the equality lookup. The operator suffix selects from a **closed, per-field operator allowlist** carried in `ModelMetadata.allowlists.operators` ([DATA_MODELING.md §4.1](./DATA_MODELING.md)).

```python
User.objects.filter(
    email__iexact="A@B.com",      # case-insensitive equality
    age__gte=18,                   # comparison
    role__in=["admin", "staff"],   # membership
    bio__contains="rust",          # substring
    deleted_at__isnull=True,       # null check
)
```

### 4.2 Supported lookups (v0.1) — Django-API parity

This is the **authoritative v0.1 lookup allowlist**. Each lookup maps to a structural IR operator enum and a parameterized SQL form. The set deliberately matches Django's common operators ([PRD §73](./PRODUCT_REQUIREMENTS.md): "exact, contains, comparisons, null checks, boolean logic") so the migrating team is not surprised.

| Lookup | Meaning | SQL form (parameterized) | Applicable types |
|--------|---------|--------------------------|------------------|
| `__exact` / bare `=` | equality | `col = $n` | all scalar |
| `__iexact` | case-insensitive equality | `lower(col) = lower($n)` | text |
| `__contains` | substring | `col LIKE $n` (value wrapped `%v%`, escaped) | text |
| `__icontains` | case-insensitive substring | `col ILIKE $n` | text |
| `__startswith` / `__endswith` | prefix / suffix | `col LIKE $n` (`v%` / `%v`) | text |
| `__istartswith` / `__iendswith` | case-insensitive prefix/suffix | `col ILIKE $n` | text |
| `__in` | membership | `col = ANY($n)` (array bind) | all scalar |
| `__gt` `__gte` `__lt` `__lte` | comparisons | `col > $n` etc. | ordered scalar |
| `__range` | inclusive range | `col BETWEEN $n AND $m` | ordered scalar |
| `__isnull` | null check | `col IS NULL` / `col IS NOT NULL` (bool selects form; no bind) | all |
| `__ne` (alias via `exclude`) | inequality | `col <> $n` (or `NOT (…)`) | all scalar |

Design rules:
- **`__in` binds an array parameter** (`col = ANY($n)`), not an interpolated `IN (…)` list. This keeps a single bound parameter regardless of list length and is injection-safe by construction (SQL-2). Empty `__in=[]` compiles to a constant-false predicate (`false`) — documented, not an error.
- **`__contains`/`startswith`/`endswith` escape LIKE metacharacters** (`%`, `_`, `\`) in the bound value before wrapping, so a user value containing `%` matches literally. The wrapping happens in the IR/encoder, and the wrapped value is still a single bound parameter.
- **`__isnull` carries no bound value** — the boolean kwarg selects the `IS NULL` vs `IS NOT NULL` operator enum at IR-build time; nothing user-supplied reaches an identifier or operator position.
- Operator applicability is validated against the field's type-derived operator set: e.g. `__contains` on an `int` column is rejected at compile with a structured error before SQL exists (§5.3, QE-1).

**Explicitly deferred (v0.2 seams, not v0.1):** regex lookups (`__regex`/`__iregex`), JSONB path lookups (`field__key`), array containment, full-text `search`, and date-part lookups (`__year`, `__date`). The IR operator enum is **open for additive extension** (new variants are a minor IR version bump), so adding these later does not break the contract (Schema Evolution). Listing them here is a YAGNI guard, not a backlog commitment.

### 4.3 Filter/exclude/Q semantics → predicate IR

The Python builder lowers all filtering into a small **predicate tree** in the IR:

```text
PredicateNode =
  | Comparison { field_ref: FieldId, op: OperatorId, value_ref: ParamId | NoValue }
  | And [PredicateNode, …]
  | Or  [PredicateNode, …]
  | Not  PredicateNode
```

- `filter(a=1, b=2)` → `And[Cmp(a,eq,$1), Cmp(b,eq,$2)]`.
- `exclude(a=1)` → `Not(Cmp(a,eq,$1))`.
- `filter(Q(a=1) | Q(b=2))` → `Or[Cmp(a,eq,$1), Cmp(b,eq,$2)]`.
- Successive `filter()` calls → top-level `And` of each call's group (Django semantics).

`FieldId` and `OperatorId` are **indices/enums resolved from `ModelMetadata` allowlists** at IR-build time, not strings ([DATA_MODELING.md §4.2](./DATA_MODELING.md); ADR-002). `value_ref` points into the out-of-band bound-parameter array. This is what makes parameterization and allowlisting **structural rather than conventional** (Defense in Depth) — the Rust compiler literally cannot receive a user identifier string in a SQL identifier slot.

### 4.4 Invalid filters fail before SQL (acceptance criterion)

Per [PRD §397](./PRODUCT_REQUIREMENTS.md) ("Invalid filters fail before hitting SQL") and [ARCHITECTURE.md §6.4](./ARCHITECTURE.md):

- **Unknown field** (`filter(nonexistent=1)`): rejected at IR-build (Python) when resolving the field name against the allowlist — fastest fail, no Rust call needed. The Rust validator re-checks defensively (belt-and-suspenders).
- **Unsupported operator for type** (`filter(age__contains=…)`): rejected at the Rust IR validator with a `FerrumCompileError` naming field + operator + category `compilation`.
- **Bad sort direction / unknown order field**: rejected the same way (sort directions are an enum; unknown field hits the allowlist).
- All such errors carry **field/operator names but never the submitted value** (LOG-2, [DATA_MODELING.md DM-4](./DATA_MODELING.md)).

---

## 5. SQL Generation Strategy

### 5.1 Decision: AST-based compilation in Rust (not string templates)

**Ferrum compiles a typed IR into SQL by constructing a small SQL AST in `ferrum-core` and rendering it, rather than assembling SQL from string templates.** This is the load-bearing security and evolvability decision of the query engine.

| Strategy | Chosen? | Rationale |
|----------|---------|-----------|
| **Typed IR → SQL AST → render (Rust)** | **Yes** | Identifiers are enum/index references resolved from metadata; values are positional placeholders. Injection-safety is *structural*: a user string has no representation that can land in an identifier or operator position. Matches [ARCHITECTURE.md §5.4, §8.3](./ARCHITECTURE.md), SQL-1/SQL-2. |
| String templates / f-strings in Python | No | Any template that concatenates user-influenced text into SQL is one bug away from injection (convention, not structure). Splits the security model across languages. Rejected by [ARCHITECTURE.md §5.5](./ARCHITECTURE.md). |
| Dict-based IR to Rust | No | No structural guarantee of parameterization; weak typing makes versioned evolution fragile (ADR-002 alternative, rejected). |
| Full external query-builder dependency | No | Larger native surface and dialect coupling we do not control (Blast Radius); v0.1 needs only the PostgreSQL subset. |

**Why AST and not "just parameterize":** parameterization protects *values*. The harder problem is *identifiers* (table, column, operator, sort direction), which cannot be bound parameters in SQL. The AST approach solves this by making identifiers **resolved references into immutable metadata** — never user-supplied strings (SQL-1). The two mechanisms together (allowlisted identifiers + bound values) are the complete injection defense ([ARCHITECTURE.md §11.1 Layers 2–3](./ARCHITECTURE.md)).

### 5.2 What the compiler emits

`compile(&ModelMetadata, &QuerySetIR) -> Result<CompiledQuery, CompileError>` is a **pure function** ([ARCHITECTURE.md §5.4](./ARCHITECTURE.md)) returning:

```text
CompiledQuery {
    sql_text:           String        # PostgreSQL with $1,$2,… placeholders only
    bound_params:       [Value]       # positional, out-of-band from identifiers
    param_type_summary: [PgTypeTag]   # per-param PG type tags (Tier A safe; no values)
    fingerprint:        String        # stable hash of sql_text shape (Tier A)
}
```

- `sql_text` contains **only** placeholders for every value; no user value is ever inside it (SQL-2; release qualification asserts no user input appears in `sql_text`, [PRD §122](./PRODUCT_REQUIREMENTS.md)).
- `fingerprint` is a stable digest of the SQL *shape* (identifiers + placeholder positions, no values) used as the Tier A observability key ([ARCHITECTURE.md §13.2](./ARCHITECTURE.md)) and as a future plan-cache key (§8).
- `param_type_summary` lets hooks report parameter shape (count + PG types) **without exposing values** (Tier A: `param_count`, `param_type_summary`).

### 5.3 Validation-before-emission

The compiler validates the IR against metadata **before** emitting any SQL ([ARCHITECTURE.md §8.3](./ARCHITECTURE.md), §11.1 Layer 2):

1. **IR version check** — incompatible `version` fails fast (Evolutionary Architecture).
2. **Identifier resolution** — every `FieldId`/`OperatorId`/sort enum must resolve in the metadata allowlist; unknown → `CompileError` (no SQL).
3. **Operator/type compatibility** — operator must be in the field's permitted set (§4.2).
4. **Shape validation** — e.g. `limit`/`offset` are non-negative bound ints; `__range` has two bounds.

Only after all checks pass does the AST render. A failed validation produces a structured `CompileError { model, field, operator, category }` and **never a partial SQL string** ([ARCHITECTURE.md §6.4](./ARCHITECTURE.md)).

### 5.4 Dialect scope

SQL emission is **PostgreSQL-dialect-specific** in v0.1 ([ARCHITECTURE.md §15.2](./ARCHITECTURE.md)). Placeholders are PostgreSQL positional (`$n`). No multi-dialect abstraction (explicit non-goal). A future backend would swap the AST renderer, not the IR — the IR stays dialect-neutral where practical, which is why identifiers are references and values are typed bounds.

---

## 6. Query Compilation Pipeline

### 6.1 Stage boundaries

The pipeline has **explicit, single-responsibility stages** with a clear language/GIL boundary at each hop. This is the acceptance criterion "Query compilation pipeline has clear stage boundaries."

```text
Stage 0  BUILD            (Python, sync, no I/O)
  QuerySet chain ─► QuerySetIR (typed) + ModelMetadata ref
  • field/operator names resolved to allowlist indices HERE (first allowlist gate)
  • bound values collected into positional param array
  • danger-API guard already passed (§3.4)
        │  typed IR (serializable, versioned)
        ▼
Stage 1  VALIDATE         (Rust/ferrum-core, sync, GIL-held)
  • IR version check · identifier resolution · operator/type compat · shape
  • FAIL ⇒ CompileError, no SQL emitted
        │
        ▼
Stage 2  COMPILE          (Rust/ferrum-core, sync, GIL-held)
  • build SQL AST from validated IR
  • render sql_text with $1..$n placeholders
  • encode bound_params (LIKE-escaping, array binds) + param_type_summary
  • compute fingerprint
        │  CompiledQuery { sql_text, bound_params, param_type_summary, fingerprint }
        ▼
Stage 3  EXECUTE          (Python/asyncpg, async, cancellable)   ── §7
  • hook query_start (Tier A: fingerprint, op, model, param_count)
  • pool.acquire ─► conn.fetch(sql_text, *bound_params)
        │  raw asyncpg rows
        ▼
Stage 4  HYDRATE          (Rust decode + Python construct, sync, GIL-held)
  • Rust: row bytes ─► typed column payloads (RowPayload[])
  • Python: construct Pydantic via construct-without-revalidate (ADR-003)
        │  list[Model] | list[dict] | scalar
        ▼
Stage 5  OBSERVE          (Python, sync)
  • hook query_success (duration, row_count, Tier A) | query_failure on error
        ▼
      result returned to caller
```

### 6.2 Stage ownership, GIL, and cancellation

This table is normative and mirrors [ARCHITECTURE.md §6.2](./ARCHITECTURE.md). It is the contract for *where failures and cancellation can happen*.

| Stage | Owner | GIL | Cancellable | Failure type |
|-------|-------|-----|-------------|--------------|
| 0 Build | Python | held | n/a (no I/O) | `FerrumCompileError` (unknown field, early) / `FerrumUnscopedMutationError` |
| 1 Validate | Rust | held | **no** | `FerrumCompileError` (category `compilation`) |
| 2 Compile | Rust | held | **no** | `FerrumCompileError` |
| 3 Execute | Python/asyncpg | released | **yes** | `FerrumConnectionError`, `FerrumTimeoutError`, `FerrumIntegrityError` |
| 4 Hydrate | Rust + Python | held | no | `FerrumHydrationError` |
| 5 Observe | Python | held | n/a | hook errors isolated (never fail the query) |

**Hard concurrency rules** (from [ARCHITECTURE.md §3, §6](./ARCHITECTURE.md)):
- Stages 1–2 and 4 are **synchronous and bounded** (<1ms compile, hydrate bounded by row count). They hold the GIL but must never block on I/O — there is no tokio runtime and no waiting inside Rust.
- **All cancellable waiting is Stage 3 only.** `asyncio` timeouts and `CancelledError` are mapped at the asyncpg await point ([ARCHITECTURE.md §6.2 "Hard rule"](./ARCHITECTURE.md)). A cancellation between compile and execute simply means no SQL ran; a cancellation during execute is surfaced as `FerrumTimeoutError`/`CancelledError` and the connection is returned to the pool by the executor.
- Compile output (Stage 2) is **owned per call** — never shared between in-flight tasks (invariant 6). Two concurrent `await qs.all()` calls each get their own `CompiledQuery`.

### 6.3 IR contract shape (ADR-002 binding)

The `QuerySetIR` is the typed, versioned, serializable boundary object. Its query-engine shape (extending [ARCHITECTURE.md §5.4](./ARCHITECTURE.md)):

```text
QuerySetIR {
    version:     int                      # incompatible ⇒ fail fast at boundary
    operation:   Select | Insert | Update | Delete | Count | Exists
    table_ref:   TableId                  # index into ModelMetadata
    projection:  AllColumns | Columns[ColumnId] | CountStar | ExistsProbe
    predicate:   Option<PredicateNode>    # the §4.3 tree (None ⇒ no WHERE; guarded for mutations)
    order_by:    [(ColumnId, AscDesc)]    # enum direction
    limit:       Option<ParamId>          # bound, not literal
    offset:      Option<ParamId>          # bound, not literal
    write_values: Option<[(ColumnId, ParamId)]>  # for Insert/Update
    params:      [Value]                  # positional bound-param array (out-of-band)
}
```

Invariants that engineers must preserve:
- **No string identifiers.** Every `*Id` is an index/enum resolved from `ModelMetadata` — never a raw user string ([DATA_MODELING.md §4.2](./DATA_MODELING.md); SQL-1).
- **Values only in `params`.** No value appears anywhere except the positional `params` array referenced by `ParamId` (SQL-2).
- **Mutations carry a predicate or a danger flag.** An `Update`/`Delete` IR with `predicate: None` is only constructible via the danger API (§3.4); the Python builder refuses to emit an unscoped-mutation IR otherwise (MIG-5).
- **Versioned.** `version` mirrors `ModelMetadata.metadata_version`; a mismatch fails at the boundary, not mid-compile.

---

## 7. Async Execution Path (asyncpg)

### 7.1 Driver placement (ADR-001)

The PostgreSQL driver is **Python-side `asyncpg`**; Rust stays off the I/O path ([ARCHITECTURE.md ADR-001, §8.4](./ARCHITECTURE.md)). The query engine's execution stage (Stage 3) is the only component that touches a connection.

### 7.2 Execution interface

```python
class ConnectionExecutor:
    async def fetch(self, compiled: CompiledQuery) -> list[asyncpg.Record]: ...
    async def execute(self, compiled: CompiledQuery) -> str: ...   # returns status tag (row count for UPDATE/DELETE)
    async def fetchrow(self, compiled: CompiledQuery) -> asyncpg.Record | None: ...
```

Execution sequence for a terminal op:

```text
1. compiled = compile(metadata, ir)                 # Stages 1–2 (Rust, sync)
2. emit hook query_start (Tier A: fingerprint, op, model, param_count)
3. async with pool.acquire() as conn:               # asyncpg pool (Python-owned)
4.     rows = await conn.fetch(compiled.sql_text, *compiled.bound_params)
5. payloads = hydrate(metadata, rows)               # Stage 4 (Rust decode)
6. instances = [Model.model_construct(**p) for p in payloads]   # ADR-003
7. emit hook query_success (duration, row_count, Tier A)
```

- The pool is the **only shared mutable runtime resource**, owned and synchronized by asyncpg/Python ([ARCHITECTURE.md §6.3](./ARCHITECTURE.md)). Pool size and timeouts are config ([ARCHITECTURE.md §14](./ARCHITECTURE.md): 10–50 connections typical).
- `bound_params` are passed to asyncpg as native positional args — asyncpg itself does server-side parameter binding (no client-side string interpolation), which is the final structural injection guarantee at the wire.

### 7.3 Transactions

Transaction boundaries are Python-owned ([ARCHITECTURE.md §5.1](./ARCHITECTURE.md)). The query engine participates but does not own transaction lifecycle:

```python
async with Ferrum.transaction() as tx:
    user = await User.objects.using(tx).create(email="a@b.com")
    await Account.objects.using(tx).create(owner_id=user.id, currency="USD")
# commit on clean exit; rollback on exception (asyncpg transaction)
```

- `using(tx)` binds a QuerySet's execution to a specific connection/transaction instead of a fresh pool acquire. It is a lazy IR-neutral binding (no compile change).
- Rollback is driven by exceptions in Python (Blast Radius: failure scope owned where I/O lives). The Rust compile stage is transaction-agnostic — it never holds a connection.

### 7.4 Failure modes and mapping

All driver/PostgreSQL failures pass through the **centralized error boundary** ([ARCHITECTURE.md §8.8, ADR-006](./ARCHITECTURE.md)) and are mapped to the sanitized Ferrum taxonomy. The query engine never surfaces a raw asyncpg/PG error to the caller.

| Failure | Stage | Surface | Sanitization |
|---------|-------|---------|--------------|
| Unknown field/operator | 1 | `FerrumCompileError` | field/operator names only, no value |
| Pool exhaustion / acquire timeout | 3 | `FerrumConnectionError` | host/port/db/user category only, never DSN/password (CRED-1) |
| Statement timeout / `CancelledError` | 3 | `FerrumTimeoutError` | no row data |
| Constraint violation (unique/FK/check) | 3 | `FerrumIntegrityError` | constraint/field name; PG `DETAIL`/`HINT` row values stripped (ERR-1, [DATA_MODELING.md §8.4](./DATA_MODELING.md)) |
| Row decode / type mismatch | 4 | `FerrumHydrationError` | column/type category, no row body |
| Rust panic | 1/2/4 | `FerrumInternalError` (catchable) | no addresses/paths (ERR-2) |

**Concurrency/perf note explicitly called out:** because cancellation is confined to Stage 3, a timeout cannot leave Rust mid-compile or corrupt shared metadata (it is immutable anyway). A connection caught in a cancelled `fetch` is released back to the pool by the executor's `async with` scope; the failure mode is bounded to one task and one connection (Blast Radius).

---

## 8. Optimization Opportunities (hooks identified, not implemented in v0.1)

Per the acceptance criterion "Optimization hooks are identified even if not implemented in v0.1," this section names the seams and fixes the contracts so adding them later is additive (Evolutionary Architecture). **Engineers must not build these in v0.1** — doing so violates YAGNI and PRD scope.

### 8.1 Plan / compiled-query caching (seam)

- **Seam:** `fingerprint` (§5.2) is a stable key over SQL shape. A future `(fingerprint → CompiledQuery)` cache keyed off immutable metadata avoids recompiling identical query shapes.
- **Constraint:** the cache stores SQL text + param *shape* only — **never bound values** (Tier A discipline). Cache is per-process, read-mostly, lock-light.
- **Why deferred:** compile is already <1ms p99 ([ARCHITECTURE.md §14](./ARCHITECTURE.md)); caching is a measured optimization, not a v0.1 need.

### 8.2 Relationship loading: `select_related` / `prefetch_related` (v0.2 seam)

Bound by [DATA_MODELING.md §7.3](./DATA_MODELING.md)'s async contract. When relationships land:

| Strategy | API (v0.2) | Compilation | N+1 |
|----------|------------|-------------|-----|
| `select_related("author")` | one awaited query | single SQL with `JOIN`; co-hydrate parent+related | avoids |
| `prefetch_related("comments")` | one awaited query set | **second batched query** keyed by parent FK ids (`WHERE fk = ANY($1)`) | avoids |
| explicit fetch (default) | `await post.author.fetch()` | one awaited query per call | app-managed |

- **IR readiness:** the predicate tree already supports `__in`/`= ANY($n)`, which is exactly the batched-prefetch shape — so prefetch is an additive IR/loader feature, not a redesign.
- **Hard async rule (inherited):** no implicit synchronous lazy load; all DB traversal is explicit `await` ([DATA_MODELING.md §7.3](./DATA_MODELING.md)). v0.1 N+1 is the application's responsibility ([ARCHITECTURE.md §14](./ARCHITECTURE.md)).

### 8.3 `EXPLAIN` integration (seam)

- **Seam:** a dev-only `await qs.explain()` that prepends `EXPLAIN [ANALYZE]` to the compiled SQL and returns the plan text.
- **Security boundary:** `EXPLAIN ANALYZE` executes the query and its output can contain row-derived values — therefore it is **local-dev only**, gated like Tier C observability ([ARCHITECTURE.md §13.2](./ARCHITECTURE.md)), never exposed via a production query-inspection endpoint (no production HTTP query inspection — [ARCHITECTURE.md §17 "Do not implement"](./ARCHITECTURE.md)).
- **Why deferred:** it is a debugging affordance, not a v0.1 acceptance requirement.

### 8.4 Streaming / server-side cursors (seam)

- **Seam:** `async for row in qs` over an asyncpg server-side cursor for large result sets, bounding memory instead of materializing `all()`.
- **Contract:** same compile path; only the execute+hydrate stages stream in batches. Hydration stays per-batch in Rust (Data Gravity).
- **Why deferred:** v0.1 targets OLTP row counts ([ARCHITECTURE.md §14](./ARCHITECTURE.md)); bulk/streaming is a v0.2 path.

### 8.5 Bulk operations and aggregation (seam)

- **Bulk insert/update** (`bulk_create`, `bulk_update`) compile to multi-row `INSERT`/`UPDATE … FROM (VALUES …)` with array binds — reserved for v0.2 (PRD bulk ops deferred).
- **Aggregation/group-by** (`annotate`, `aggregate`) extends the IR projection with aggregate nodes — a documented additive IR extension, not v0.1 scope.

---

## 9. Security Requirements for Engineers (query-path surface)

These map the PRD/ARCHITECTURE/DATA_MODELING security gates to the query engine. They are **release-qualification gates** and must be test-covered ([PRD §122](./PRODUCT_REQUIREMENTS.md): coverage proving user input is not interpolated into SQL across create/filter/order/update/delete).

| ID | Requirement | Enforcement point | Test |
|----|-------------|-------------------|------|
| QE-1 | Unknown field/operator/sort fails before SQL is emitted | Stage 0 (Python allowlist) + Stage 1 (Rust validator) | `filter(bad=1)` / `age__contains` → `FerrumCompileError`, assert no SQL produced |
| QE-2 | Values are bound parameters only; never in `sql_text` | Stage 2 compiler + IR shape | Fuzz values (incl. `'; DROP TABLE`) → assert value absent from `sql_text`, present only in `bound_params` |
| QE-3 | `__in` binds an array; `__contains` escapes LIKE metachars | Bound-param encoder (§4.2) | `__in` of any length = 1 param; `%`/`_` in value matched literally |
| QE-4 | No raw SQL escape hatch on the query path | API surface (§3.5) | No `extra()`/string-fragment/template API exists |
| QE-5 | Filter/sort enums validated before compile (Django parity preserved, structurally) | Stage 1 validator | Unsupported operator for type → structured error |
| QE-6 | Unscoped `update()`/`delete()` fail without the named danger API | Stage 0 guard (§3.4) | `User.objects.delete()` raises before IR; `danger_delete_all()` permitted |
| QE-7 | Default hook payloads are Tier A; no bound values under any key | Hook dispatcher (§7.2) | Payload schema test asserts no value strings; `param_type_summary` present |
| QE-8 | PG/driver errors sanitized; no DSN/password/row values | Error boundary (§7.4, ADR-006) | Fixture DSN + constraint violation → exception has no password/row substring |
| QE-9 | `explain()` (when built) is local-dev gated, never production-exposed | §8.3 gate | `explain()` blocked outside local-dev guard; no prod query-inspection endpoint |

**SecurityEngineer notification:** the SQL compiler (SQL-1/SQL-2), the bound-parameter encoder (`__in`/LIKE escaping), the unscoped-mutation danger guard (MIG-5/QE-6), and the Tier A hook payload schema (LOG-1/QE-7) require SecurityEngineer review at v0.1 release qualification. This complements the SQL-compilation review already flagged in [ARCHITECTURE.md §11.2](./ARCHITECTURE.md) and the identifier/expression allowlisting review in [DATA_MODELING.md §10](./DATA_MODELING.md).

---

## 10. End-to-End Example (v0.1)

```python
from ferrum import Model, Field, Q
from datetime import datetime

class Post(Model):
    id: int = Field(primary_key=True)
    author_id: int = Field(references="users.id")
    title: str = Field(max_length=200)
    published: bool = False
    created_at: datetime

# --- READ: lazy build, single await ---
qs = (
    Post.objects
    .filter(published=True)                       # AND group
    .filter(Q(title__icontains="rust") | Q(title__icontains="python"))  # OR subtree
    .exclude(author_id__in=[1, 2, 3])             # NOT(= ANY($n))
    .order_by("-created_at")                       # DESC enum
    .limit(20)                                     # bound param
)                                                  # ← still NO I/O

recent = await qs.all()                            # ← compile + execute + hydrate HERE
# SQL (shape): SELECT … FROM posts
#   WHERE published = $1
#     AND (title ILIKE $2 OR title ILIKE $3)
#     AND NOT (author_id = ANY($4))
#   ORDER BY created_at DESC LIMIT $5
# bound_params = [True, "%rust%", "%python%", [1,2,3], 20]   ← values out-of-band only

# --- COUNT / EXISTS ---
n = await Post.objects.filter(published=True).count()
any_drafts = await Post.objects.filter(published=False).exists()

# --- WRITE ---
post = await Post.objects.create(author_id=42, title="Hello", published=True)

# --- SCOPED mutation (ok) ---
await Post.objects.filter(author_id=42, published=False).delete()

# --- UNSCOPED mutation (blocked) ---
# await Post.objects.delete()              # raises FerrumUnscopedMutationError (no SQL)
await Post.objects.danger_delete_all()     # explicit, audited

# --- INVALID filter (blocked before SQL) ---
# await Post.objects.filter(nonexistent=1).all()   # FerrumCompileError, no SQL
# await Post.objects.filter(author_id__contains=1).all()  # operator/type mismatch
```

Every value above travels in `bound_params`; every identifier is an allowlist-resolved reference; the only "relationship" is the explicit `author_id` column — fully consistent with [DATA_MODELING.md](./DATA_MODELING.md) and PRD scope.

---

## 11. Alternatives Considered

| Decision | Chosen | Alternative | Why rejected |
|----------|--------|-------------|--------------|
| SQL generation | Typed IR → SQL AST in Rust | String templates / f-strings in Python | Templates make injection-safety conventional, not structural; splits security model ([ARCHITECTURE.md §5.5](./ARCHITECTURE.md)) |
| IR contract | Typed, versioned struct (ADR-002) | Dict-based IR | No structural parameterization guarantee; fragile evolution |
| Identifier handling | Enum/index references from metadata | Pass field-name strings to Rust | Strings in identifier position are the core injection risk (SQL-1) |
| `__in` lowering | Array bind (`= ANY($n)`) | Interpolated `IN (v1, v2, …)` | List interpolation reintroduces injection + unstable fingerprint/param count |
| Driver placement | Python `asyncpg` (ADR-001) | Rust async driver (sqlx/tokio) | Dual runtime under one GIL; harder cancellation (Blast Radius) |
| Cancellation point | Stage 3 (Python await) only | Cancellable Rust compile | Compile is sub-ms and pure; cancellable Rust complicates GIL/panic semantics |
| Boolean logic surface | `AND`/`exclude` + `Q` (`& \| ~`) | Full predicate DSL / arbitrary expressions | `Q` matches Django mental model; broader DSL is speculative (YAGNI) |
| Relationships/prefetch | Forward seam only (§8.2) | Build loaders in v0.1 | PRD Won't-have; loaders are v0.2 (YAGNI) |
| Result caching | Deferred seam (§8.1) | Implicit per-QuerySet cache | Compile already <1ms; caching is a measured optimization |
| `get()` multiplicity | `LIMIT 2` + Python check | Two queries / trust client | One round trip, detects multiplicity, no client trust |

---

## 12. Open Items & Handoff

| Item | Owner | Blocks |
|------|-------|--------|
| Formal ADR records (esp. ADR-002 IR contract, ADR-003 hydration) in `DECISIONS.md` | ChiefArchitect ([GUY-74](/GUY/issues/GUY-74)) | ADR sign-off (this doc consumes ADR-002/003) |
| Migration DDL emission (separate compile path) | [MIGRATIONS.md](./MIGRATIONS.md) ([GUY-73](/GUY/issues/GUY-73)) | migration impl (parallel; shares the AST-in-Rust principle) |
| SecurityEngineer review of compiler/encoder/danger-guard/hook schema (QE-1,2,3,6,7,8) | SecurityEngineer | release qualification |
| CEO/board approval of query-engine contract | CEO | query implementation start |

### Acceptance criteria coverage (GUY-72)

- **QuerySet design (lazy evaluation, chaining, result types)** — §3 (lazy/immutable builder, chainable methods, terminal coroutines, result-type contract).
- **Filter system (field lookups: `__eq`, `__contains`, `__in`, `__gt`, `__lt`, etc.)** — §4 (closed lookup allowlist with Django-API parity, predicate-tree IR, fail-before-SQL).
- **SQL generation strategy (AST-based vs string templates — justified)** — §5 (AST-in-Rust chosen; structural injection-safety rationale; alternatives in §11).
- **Query compilation pipeline (Python IR → Rust SQL builder → parameterized SQL)** — §6 (explicit Stage 0–5 boundaries, GIL/cancellation ownership, IR contract shape).
- **Async execution path (asyncpg driver interface)** — §7 (executor interface, transactions, cancellation at Stage 3, sanitized failure mapping).
- **Optimization opportunities (batching, prefetch_related, explain)** — §8 (plan cache, select/prefetch, EXPLAIN, streaming, bulk/aggregation — all identified as seams, none built).
- **Filter lookups match Django's API surface** — §4.2 (operator table).
- **SQL generation is safe against injection (parameterized only)** — §5.1, §5.2, §9 (QE-2/QE-3), structural identifier+value separation.
- **Query compilation pipeline has clear stage boundaries** — §6.1–§6.3.
- **Optimization hooks identified even if not implemented in v0.1** — §8.
- **Document committed to `/docs/foundation/QUERY_ENGINE.md`** — this file.

### Engineer handoff

Implementation of the query path may begin once this document and `DECISIONS.md` (ADR-002, ADR-003) are approved, following the slice order in [ARCHITECTURE.md §17](./ARCHITECTURE.md): `ferrum-core` IR types + compile unit tests first (Stages 1–2 with the §9 security tests), then the PyO3 boundary, then the Python lazy builder (Stage 0) and read path (`filter` → `all`, Stages 3–5), then write path + danger API.

**Do not implement (architecture/scope guard):** relationship loaders, `select_related`/`prefetch_related`, result caching, `explain()` outside a local-dev gate, server-side-cursor streaming, bulk ops, aggregation/group-by, or any raw-SQL escape hatch. These are deferred seams (§8), not v0.1 work.

---

*Produced by Chief Architect for GUY-72. Pending CEO/board query-engine approval.*
