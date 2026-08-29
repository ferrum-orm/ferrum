---
task_id: w2-b-query-expressiveness
run_id: 20260829T110000Z
authority: SecurityEngineer
reviewer: security-engineer
reviewed_at: 2026-08-29T21:30:00Z
base_revision: 612f476c32fa7b1fbd38e4dc9f4c689d05b72191
decision: approved
scope:
  - python/ferrum/queryset.py
  - python/ferrum/expressions.py
  - crates/ferrum-sql/src/emit.rs
  - tests/python/security/test_sql_safety.py
---

# Named Authority Verdict

## Authority

SecurityEngineer

## Claims reviewed

1. Identifiers stay metadata-allowlisted (resolved against `field_index` /
   relation metadata); no user-supplied identifiers reach SQL.
2. Values stay bound parameters (IR carries `BindValue` dicts); no value
   interpolation.
3. No `raw()`, `extra()`, or string fragments introduced.
4. Write-scope safety preserved (`_check_write_scope`, `is_write=True`,
   `select_for_update` restrictions).
5. No SQL injection vectors introduced by `F`/`Star`/`Combinable`.

## Evidence

### Identifiers stay metadata-allowlisted

`git diff HEAD -- python/ferrum/expressions.py`:

- `F.__init__` validates `name` is a non-empty `str` — rejects any non-string
  or empty input with `ValueError`. ✅
- `resolve_field_name(value: str | F) -> str` returns the field name string;
  raises `TypeError` for any other type. Never accepts raw SQL. ✅

`git diff HEAD -- python/ferrum/queryset.py`:

- `group_by` (queryset.py:883): calls `resolve_field_name(field)` then checks
  `if field not in field_names` (metadata allowlist). Unknown field →
  `FerrumCompileError` "Unknown field". ✅
- `date_trunc` (queryset.py:902): calls `resolve_field_name(field)` then
  validates against metadata. ✅
- `order_by` (queryset.py:847): `F` → appends `f.name` to `_order_by`;
  validated against `field_index` in `_build_ir`. ✅
- `Aggregate.sum/avg/min/max` (queryset.py:120-134): call
  `resolve_field_name(field)` before constructing the `Aggregate`. ✅
- `Aggregate.count` (queryset.py:112-118): `Star` → `field = None` (COUNT(*));
  `F` → `field = field.name`. No SQL fragment. ✅

All `F`-resolved field names are validated against the model metadata
allowlist (`field_index`) before reaching the IR. No user-supplied
identifier reaches SQL. ✅

### Values stay bound parameters

IR carries `BindValue` dicts (queryset.py:1049, 2197, 2502, 2575, 2627 emit
`"version": _IR_VERSION` and bound params). `test_ir_does_not_contain_raw_sql`
(test_query_expressiveness.py:347) asserts no SQL keywords (`SELECT`/`WHERE`/
`FROM`) appear in the IR repr. ✅

`git diff HEAD -- crates/ferrum-core/src/ir/mod.rs`: empty — no IR schema
changes that could introduce value interpolation. ✅

### No raw()/extra()/string fragments

```
$ grep -rn "def raw\|def extra" python/ferrum/queryset.py python/ferrum/expressions.py
(no output)
```

`test_no_raw_method_exists` (test_query_expressiveness.py:342) asserts
`not hasattr(qs, "raw")` and `not hasattr(qs, "extra")`. ✅

`F` and `Star` carry only field-name strings (validated non-empty `str`); they
never carry SQL fragments. `Combinable` docstring explicitly states
"never carry raw SQL fragments". ✅

This satisfies AGENTS.md §2.9 and §3 (SQL safety: user input never
interpolated into SQL identifier or value positions). ✅

### Write-scope safety preserved

- `_check_write_scope()` (queryset.py:3219): preserved, called by `delete()`
  (3291), `update()` (3422), `update_returning()` (3487). ✅
- `is_write=True` on write terminals (queryset.py:2340, 2710, 3091, 3193,
  3510): preserved. ✅
- `select_for_update()` (queryset.py:1789): preserved; `_check_write_scope`
  rejects combining FOR UPDATE with write APIs. ✅
- Relation-filter lookups rejected in UPDATE/DELETE via `_check_write_scope`
  (unchanged). ✅

### No SQL injection vectors

`F`/`Star` introduce no new attack surface:

- `F(name)` → `name` must be a non-empty `str` → resolved to a plain field
  name → validated against metadata allowlist. An attacker cannot inject SQL
  through `F` because the field name must exist in the model's `field_index`.
- `Star()` → carries no data; maps to `COUNT(*)` only.
- `resolve_field_name` rejects non-str/non-F types with `TypeError`.
- No comparison operators on `F` (no `F("a") > F("b")`) — field-to-field
  comparison would require a new IR node and is a documented blocker.

### Security tests

```
$ uv run pytest tests/python/security/test_sql_safety.py -x -q -m ""
74 passed in 0.23s
```
Exit: 0 ✅

### emit.rs cast-matrix change (D1 fix)

`FieldType::Text | FieldType::Enum | FieldType::Domain => "text"` — this is
a compile-time constant string literal in the Rust emitter. No user input
reaches this path; `FieldType` is derived from model metadata. Not a security
surface. ✅

## Findings

No blocking findings. No security-sensitive changes (auth, secrets,
SQL-compilation escape hatches, migration apply) beyond the SQL-compilation
identifier allowlisting already verified. The change preserves:

- §3 SQL safety (identifiers allowlisted, values bound, no raw SQL).
- §3 error boundaries (no new error surfaces).
- §3 credential handling (no secrets in payloads).

## Decision

`approved`

This record grants only the SecurityEngineer gate. It does not substitute for
another authority or independent verification.
