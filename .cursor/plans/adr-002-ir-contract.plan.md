# ADR-002: QuerySet IR Contract (Canonized v1)

**Status:** Accepted (Wave 0 canonization)  
**Date:** 2026-06-13  
**Deciders:** chief-architect (Wave 0 governance)  
**Supersedes:** Informal "ADR-002 in progress" notes in `crates/ferrum-core/src/ir/mod.rs`

---

## Context

Ferrum crosses the Python↔Rust boundary with a typed, versioned intermediate representation. The architecture ([ARCHITECTURE.md §5.4](../.claude/docs/ARCHITECTURE.md)) requires:

- Identifiers resolved from metadata allowlists — never user strings in SQL identifier positions.
- Values carried out-of-band as bound parameters.
- Explicit `version` field; incompatible versions fail fast at the boundary.
- Pure compilation: `compile(&ModelMetadata, &QuerySetIR) -> Result<CompiledQuery, CompileError>`.

Implementation already exists in `crates/ferrum-core/src/ir/` with `IR_VERSION = 1` and JSON serialization over PyO3 (`compile_query(metadata_json, ir_json)` in `crates/ferrum-pyo3/src/lib.rs`). **This document canonizes that implementation** — it does not redesign the contract.

---

## Decision

**Adopt IR version 1** as the authoritative cross-boundary contract, serialized as **JSON strings** at the PyO3 boundary until a binary codec is justified (post-v0.1).

| Constant | Value | Location |
|----------|-------|----------|
| `IR_VERSION` | `1` | `crates/ferrum-core/src/ir/mod.rs` |
| Serialization | JSON (UTF-8) | Python `json.dumps` ↔ Rust `serde_json` |
| Boundary function | `compile_query(metadata_json, ir_json) -> dict` | `crates/ferrum-pyo3/src/lib.rs` |

---

## Field Index Rules

### `FieldRef`

Every column reference in the IR uses a **pre-validated index** into `ModelMetadata.fields`:

```json
{ "name": "email", "index": 1 }
```

| Rule | Enforcement |
|------|-------------|
| `index` is the authoritative identifier for compilation | Rust resolves `metadata.fields[index].column_name` for SQL |
| `name` is diagnostic only | Used in `CompileError` messages; must match `fields[index].name` when both present |
| Unknown index (`index >= fields.len()`) | `CompileError::UnknownField` before SQL emission |
| Index tampering (valid index, wrong name) | Rust trusts **index only**; Python builder must set both consistently |

**Python builder obligation (Stage 0):** Resolve user-facing field names against the model allowlist at IR-build time. Unknown fields fail in Python when possible; Rust re-validates defensively.

### `ModelMetadata.fields`

Ordered, immutable list built at class-definition time:

```json
{
  "model_name": "User",
  "table_name": "users",
  "pk_index": 0,
  "fields": [
    {
      "name": "id",
      "column_name": "id",
      "field_type": "int",
      "allowed_operators": ["eq", "gt", "lt"],
      "nullable": false
    }
  ]
}
```

| Field | Purpose |
|-------|---------|
| `table_name` | SQL `FROM` target (validated at model def, not runtime user input) |
| `column_name` | SQL column identifier (from metadata, quoted at emit time) |
| `field_type` | Hydration codec tag (`int`, `bigint`, `text`, `bool`, `datetime`, `uuid`, `bytes`, `json`, …) |
| `allowed_operators` | Closed set per field; filter `operator` must be in this list |
| `pk_index` | Primary key field index |

---

## `BindValue` Shapes

Bound parameter values are **never** interpolated into SQL text. They appear only in:

1. Inline `Filter.value` / `Operation::Insert|Update` value slots (v1), and
2. The parallel `bound_params` list in `CompiledQuery` output (emit order).

### Serde JSON encoding (externally tagged + adjacently tagged content)

```json
{ "type": "null" }
{ "type": "bool", "value": true }
{ "type": "int", "value": 42 }
{ "type": "float", "value": 3.14 }
{ "type": "text", "value": "user@example.com" }
{ "type": "bytes", "value": [1, 2, 3] }
{ "type": "datetime", "value": "2026-06-13T12:00:00Z" }
```

| Variant | Rust type | Python source | Notes |
|---------|-----------|---------------|-------|
| `null` | `BindValue::Null` | `None` | |
| `bool` | `BindValue::Bool` | `bool` | |
| `int` | `BindValue::Int(i64)` | `int` | Widen Python int; reject overflow at build |
| `float` | `BindValue::Float` | `float` | |
| `text` | `BindValue::Text` | `str` | User strings **only** here |
| `bytes` | `BindValue::Bytes` | `bytes` | Serialized as JSON byte array |
| `datetime` | `BindValue::Datetime` | `datetime` → ISO-8601 str | Decoded by asyncpg on Python side |

**v1 extension path:** New variants (e.g. `uuid`, `array`) are additive if old deserializers ignore unknown tags — prefer minor IR doc bump before wire change.

---

## `Operation` Enum

Externally tagged with `"kind"` (snake_case):

### `select`

```json
{
  "kind": "select",
  "fields": [{ "name": "id", "index": 0 }, { "name": "email", "index": 1 }]
}
```

Empty `fields` is invalid for v1 SELECT (compile error).

### `insert`

```json
{
  "kind": "insert",
  "values": [
    [{ "name": "email", "index": 1 }, { "type": "text", "value": "a@b.com" }]
  ]
}
```

Note: serde serializes tuple pairs as JSON arrays.

### `update`

```json
{
  "kind": "update",
  "assignments": [
    [{ "name": "email", "index": 1 }, { "type": "text", "value": "new@b.com" }]
  ]
}
```

Requires non-empty `filters` OR explicit danger-API path (Python guard before IR build; MIG-5).

### `delete`

```json
{ "kind": "delete" }
```

Requires non-empty `filters` OR danger-API path.

### Deferred operations (Wave 1–2; same `IR_VERSION` additive)

| Operation | Planned shape | Notes |
|-----------|---------------|-------|
| `count` | `{ "kind": "count" }` | Reuses filters; no projection fields |
| `exists` | `{ "kind": "exists" }` | EXISTS subquery |

Adding variants is **backward compatible** within v1 if deserializers use `#[serde(tag = "kind")]` and callers check operation kind at compile time.

---

## `QuerySetIR` Root Document

Full v1 schema (authoritative — matches `crates/ferrum-core/src/ir/mod.rs`):

```json
{
  "version": 1,
  "model_name": "User",
  "operation": {
    "kind": "select",
    "fields": [
      { "name": "id", "index": 0 },
      { "name": "email", "index": 1 }
    ]
  },
  "filters": [
    {
      "field": { "name": "email", "index": 1 },
      "operator": "eq",
      "value": { "type": "text", "value": "test@example.com" }
    }
  ],
  "order_by": [
    {
      "field": { "name": "id", "index": 0 },
      "direction": "asc"
    }
  ],
  "limit": 10,
  "offset": null
}
```

### Filter semantics (v1)

- Multiple `filters` entries are **AND**-combined (flat list).
- `operator` is a **string** token from `FieldMeta.allowed_operators` (e.g. `eq`, `gt`, `icontains`, `is_null`).
- Operators `is_null` / `is_not_null` use `BindValue::Null` as placeholder; emitter must not bind a parameter for null-check ops (Wave 1 emit fix).

### Sort direction

```json
"direction": "asc" | "desc"
```

Any other value → `CompileError::InvalidSortDirection` (enum deserialization failure maps to compile error at boundary).

### Limit / offset (v1 interim)

- Typed as `u64 | null` on the IR root — **literals embedded in SQL** at emit time.
- Values originate from QuerySet chain integers (developer-controlled), not end-user SQL fragments.
- **Wave 1+ alignment:** QUERY_ENGINE.md specifies bound-parameter limit/offset; migrating to `ParamId` references is a compatible v1 extension (optional fields) — not a version bump.

---

## Operator Allowlist (v1 tokens)

Canonical operator strings validated against per-field `allowed_operators`:

| Token | SQL (after allowlist check) |
|-------|----------------------------|
| `eq` | `=` |
| `ne` | `!=` |
| `gt`, `gte`, `lt`, `lte` | comparison ops |
| `contains`, `icontains` | `LIKE` / `ILIKE` (Wave 1: wrap/escape in encoder) |
| `is_null`, `is_not_null` | `IS NULL` / `IS NOT NULL` |

Unknown operator for field → `CompileError::UnsupportedOperator`.

**Deferred (documented, not v1):** `in` (array bind), `range`, predicate tree (`And`/`Or`/`Not`), `Q` objects — require IR extension; target Wave 1 late or v2 if shape is breaking.

---

## Compiled Output Contract

`compile_query` returns a Python dict:

```json
{
  "sql_text": "SELECT \"id\", \"email\" FROM \"users\" WHERE \"email\" = $1",
  "bound_params": ["{\"type\":\"text\",\"value\":\"test@example.com\"}"],
  "param_type_summary": ["email:eq"]
}
```

| Key | Type | Tier A safe |
|-----|------|-------------|
| `sql_text` | `str` | Yes (placeholders only for values) |
| `bound_params` | `list[str]` | Values present — **never log in Tier A hooks**; hooks use `param_type_summary` + count |
| `param_type_summary` | `list[str]` | Yes |

**Wave 1 addition:** `fingerprint` (stable hash of SQL shape) — additive dict key, no IR version bump.

---

## Versioning Rules

| Change type | Action |
|-------------|--------|
| Bugfix in validation/emission | Same `IR_VERSION = 1`; patch release |
| Add optional IR field (e.g. `danger_all: bool`) | Same version if old Rust ignores unknown fields **or** field has default; document in this file |
| Add `Operation` variant (`count`, `exists`) | Same version; compile match must handle new kind |
| Add `BindValue` variant | Same version if additive |
| Predicate tree replacing flat `filters` | **Requires `IR_VERSION = 2`** + coordinated Python/Rust release |
| Binary codec replacing JSON | **Requires new transport doc**; IR semantic version may stay 1 |
| `IR_VERSION` mismatch at boundary | `CompileError::IrVersionMismatch { expected, got }` → Python `FerrumCompileError` |

**Fail-fast rule:** Rust checks `ir.version == IR_VERSION` as the **first** compile step (`compile/mod.rs`).

**Release coupling:** Python package and native wheel must ship together on IR changes. CI should include a round-trip JSON fixture test (Wave 1).

---

## Python ↔ Rust Shared Schema Checklist

Both sides MUST agree on:

- [x] Root keys: `version`, `model_name`, `operation`, `filters`, `order_by`, `limit`, `offset`
- [x] `FieldRef`: `{ name, index }`
- [x] `Filter`: `{ field, operator, value }`
- [x] `BindValue` tagged union: `type` + optional `value`
- [x] `Operation` tagged union: `kind` + payload
- [x] `SortDirection`: `"asc"` | `"desc"`
- [x] `ModelMetadata` + `FieldMeta` + `FieldType` snake_case enums
- [ ] `fingerprint` on compile output (Wave 1)
- [ ] `count` / `exists` operations (Wave 1–2)
- [ ] Predicate tree / `Q` objects (future v1.x or v2)

---

## Relationship to QUERY_ENGINE.md

QUERY_ENGINE.md §6.3 describes a **target** IR with `ParamId`, predicate trees, and bound limit/offset. The **implemented v1 contract** is intentionally simpler:

| QUERY_ENGINE target | v1 canon | Resolution |
|--------------------|----------|------------|
| Separate `params[]` array with `ParamId` | Values inline in filters | v1 acceptable; emit collects into `bound_params` in order |
| `PredicateNode` tree | Flat `filters` AND list | Wave 1+ extension; `exclude`/`Q` need tree or de Morgan lowering |
| Bound limit/offset | `u64` literals on IR root | Wave 1 alignment optional within v1 |
| `danger_flag` on IR | Python-only guard pre-IR | Preserved; unscoped mutation never emitted |

Engineers implement QUERY_ENGINE behavior **through** this v1 wire format; evolving the wire shape follows versioning rules above.

---

## Security Invariants (non-negotiable)

1. **SQL-1:** Column/table names from `ModelMetadata` indices only at emit time.
2. **SQL-2:** User values only in `BindValue` / `bound_params`; `$N` placeholders in SQL text.
3. **Version gate:** Wrong `version` → error, no SQL.
4. **Operator allowlist:** String token must be in `allowed_operators` before emit.

---

## References

- Implementation: `crates/ferrum-core/src/ir/mod.rs`, `metadata.rs`
- Boundary: `crates/ferrum-pyo3/src/lib.rs`
- Architecture: `.claude/docs/ARCHITECTURE.md` §5.4, ADR-002
- Query engine: `.claude/docs/QUERY_ENGINE.md` §5–§6
