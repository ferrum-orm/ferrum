---
task_id: w1-a-query-correctness
run_id: 20260829T080749Z
authority: SecurityEngineer
reviewer: security-engineer
reviewed_at: 2026-08-29T12:00:00Z
base_revision: 02d585513980ae89dbd6474196619a82faac460d
decision: approved
scope:
  - python/ferrum/queryset.py
  - crates/ferrum-sql/src/emit.rs
  - tests/python/security/test_sql_safety.py
---

# Named Authority Verdict

## Authority

SecurityEngineer

## Claims reviewed

1. **SQL identifier/operator emission:** Identifiers stay metadata-allowlisted;
   values stay bound parameters. Unknown fields/operators/sort directions fail
   BEFORE SQL emission (AGENTS.md §3, §2.9).
2. **`filter(field=None)` / `exclude(field=None)`:** Null-equality rewrite does not
   introduce SQL injection. The `None` value never reaches a bound parameter or
   string interpolation position.
3. **`danger_delete_all`:** Does not widen scope. The `danger=true` IR flag bypasses
   only the Rust `MissingFilter` guard; the Python `delete()` path still requires
   `_is_filtered`.
4. **Cast changes in `postgres_value_cast`:** Do not introduce type-confusion
   vulnerabilities. The casts are applied to bound-parameter placeholders only;
   identifier resolution and value binding are unchanged.
5. **Write-scope invariants:** Slice/order/limit cannot widen UPDATE/DELETE scope
   (AGENTS.md §3, task contract invariants).

## Evidence

### 1. Diff inspection (HEAD = 02d5855)

Inspected `git diff HEAD -- python/ferrum/queryset.py
crates/ferrum-sql/src/emit.rs tests/python/security/test_sql_safety.py`.

**`python/ferrum/queryset.py`** — `_normalize_null_lookup` (lines 372-400):
- Added `eq`+`None` → `is_null` and `ne`+`None` → `is_not_null` rewrites at
  lines 395-399.
- The rewrite executes BEFORE `_validate_lookup` in `_filter_dict_to_ir`
  (line 510-511) and `_kwargs_to_ir_filters` (line 541). The rewritten operator
  is re-validated against the field allowlist. Confirmed `is_null` and
  `is_not_null` are in `_ALLOWED_OPERATORS` for every field type (verified
  `models.py:99-142`).
- `exclude(field=None)` wraps the rewritten leaf in `NOT (...)` via the
  existing `~Q` mechanism (`queryset.py:599-600`: `{"kind": "not", "child":
  inner}`). The negation is a structural IR node, not string interpolation.

**`crates/ferrum-sql/src/emit.rs`** — `postgres_value_cast` (lines 1227-1257):
- `Int` → `"integer"` (was `"bigint"`), `Float` → `"real"` (was `"double
  precision"`), `ArrayInt` → `"integer[]"` (was `"bigint[]"`), `ArrayFloat`
  → `"float8[]"` (was `"double precision[]"`).
- The cast is applied as `format!("{placeholder}::{cast}")` — a bound-parameter
  placeholder annotated with a PostgreSQL type. No user input is interpolated
  into identifier positions. The cast strings are literals from the match arms,
  not derived from IR content.
- Added a comment documenting the DDL-alignment requirement (42883 on
  `t.pk = v.pk` join predicate). Internal Rust documentation, not a runtime
  payload.

**`tests/python/security/test_sql_safety.py`** — `TestMalformedInputFuzz`
(lines 370-516):
- 18 unknown-field payloads (SQL injection, null bytes, control chars).
- 15 unsupported-operator payloads (injection, control chars).
- 13 invalid-sort-direction payloads (injection, control chars).
- 8 unknown order_by field payloads.
- `test_compile_mock_not_called_on_rejection`: monkeypatches `_compile` and
  verifies the Rust compiler is never called when Stage-0 rejects.
- `test_no_sql_text_produced_for_rejected_operator`: confirms `_build_ir()`
  raises before `_compile()` is reachable.

### 2. Source-level verification (independent)

**Identifier allowlisting:** `_validate_lookup` (`queryset.py:408-436`) raises
`FerrumCompileError` for unknown fields (`field_name not in field_index`) and
unsupported operators (`operator not in allowed_ops`). The field_index and
allowed_operators are built from immutable `ModelMetadata` (built once at
class definition time per AGENTS.md §2.10).

**Sort direction allowlisting:** `_build_ir` (`queryset.py:959-964`) raises
`FerrumCompileError` when `direction not in metadata.allowed_sort_directions`.
The allowed_sort_directions is a metadata allowlist; injection payloads like
`"DESC; DROP TABLE users;--"` are not in the allowlist and are rejected.

**Rust-side validation:** `ferrum_core::compile::compile`
(`crates/ferrum-core/src/compile/mod.rs:80-128`) validates field indices
against metadata for every operation. The `MissingFilter` guard
(`compile/mod.rs:94, 114`) rejects unscoped UPDATE/DELETE unless `danger=true`.

**Null-equality emission:** The Rust emitter (`emit.rs:1183-1184`) handles
`is_null`/`is_not_null` as nullary operators:
```rust
"is_null" => (format!("{col} IS NULL"), None),
"is_not_null" => (format!("{col} IS NOT NULL"), None),
```
`col` is `dialect.quote_ident(&metadata.fields[field_ref.index].column_name)`
— a metadata-allowlisted identifier. The bound value is `None` (no parameter).
The `None` from `filter(field=None)` never reaches a bound parameter position.

**`danger_delete_all` scope:** `danger_delete_all` (`queryset.py:3123-3176`)
constructs a fresh `QuerySet(self._model)` (line 3143) — no user
filters/limits/order_by carry over. It sets `delete_ir["operation"]["danger"]
= True` on the IR (line 3145), not on the QuerySet. The Rust `compile`
function (`compile/mod.rs:110-119`) skips `MissingFilter` ONLY when `danger`
is true on the IR `Delete` operation. `delete()` (`queryset.py:3070-3074`)
still requires `_is_filtered` and raises `FerrumDangerApiError`. The emitter
(`emit.rs:477-490`) only appends `WHERE` if `build_where_sql` returns
`Some(...)`; for a fresh unfiltered QuerySet, it returns `None`, producing
`DELETE FROM "table"` with no dangling `WHERE`.

**Write-scope invariants:** `_check_write_scope` (`queryset.py:3008-3048`)
raises `FerrumCompileError` for `_limit`/`_offset`, `select_related`,
relation-lookup joins, ranking, and aggregates on UPDATE/DELETE.
`_build_delete_ir` (`queryset.py:2029-2042`) and `_build_update_ir`
(`queryset.py:1995-2027`) both strip `order_by`/`limit`/`offset` from the IR
even when the guard does not fire on `order_by` alone.

**Cast changes do not introduce type confusion:** The `postgres_value_cast`
function only produces the PostgreSQL type name appended to a bound-parameter
placeholder via `::cast`. The cast strings are compile-time literals in the
match arms, not derived from IR content or user input. The changes align casts
with DDL types, preventing 42883 failures — they close a type-confusion gap
rather than opening one.

### 3. Test execution (fresh, this run)

```
uv run pytest tests/python/security/test_sql_safety.py -x -q -m ""
```
→ 74 passed in 0.21s (PASS)

All 74 security tests pass, including the 56 new parametrized fuzz cases and
2 verification tests in `TestMalformedInputFuzz`.

## Findings

No blocking findings.

**Info (non-blocking):**
- `exclude(field=None)` emits `NOT ("col" IS NULL)` rather than the literal
  `IS NOT NULL` substring. The negation is a structural IR `{"kind": "not"}`
  node compiled by the Rust emitter — no string interpolation. Semantically
  equivalent in PostgreSQL and consistent with the existing
  `exclude(field__is_null=True)` path. Not a security defect.
- INET has no `FieldType` (documented by `test_inet_has_no_field_type`).
  Adding one would require an IR change and ChiefArchitect escalation. Out of
  scope for W1-A; not a security regression.
- The `danger=true` IR flag is the sole mechanism for bypassing the Rust
  `MissingFilter` guard. It is set only in `danger_delete_all`/
  `danger_update_all` and deserializes with `#[serde(default)]` (false by
  default). A caller cannot inject `danger=true` via `filter()` or `delete()`
  — those paths do not set the flag and the default is false. No privilege
  escalation vector.

## Decision

**approved**

All five security claims have fresh deterministic evidence:
1. Identifiers stay metadata-allowlisted; values stay bound parameters —
   verified in `_validate_lookup`, `_build_ir` sort validation, the Rust
   `compile` function, and the fuzz test suite (74 passed).
2. `filter(field=None)` / `exclude(field=None)` do not introduce SQL injection
   — the `None` value is rewritten to a nullary `is_null`/`is_not_null`
   operator before validation; the Rust emitter emits `IS NULL`/`IS NOT NULL`
   with no bound parameter; `exclude` negation is structural.
3. `danger_delete_all` does not widen scope — fresh QuerySet, IR-level flag,
   Rust `MissingFilter` guard bypassed only for the explicit danger API.
4. Cast changes do not introduce type-confusion vulnerabilities — casts are
   literal type names applied to bound-parameter placeholders; the changes
   close a 42883 gap.
5. Write-scope invariants hold — `_check_write_scope` and IR stripping prevent
   slice/order/limit from widening UPDATE/DELETE scope.

This record grants only the SecurityEngineer gate. It does not substitute for
the CodeReviewer gate or independent verification.
