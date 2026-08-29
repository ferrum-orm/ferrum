---
task_id: w2-b-query-expressiveness
run_id: 20260829T110000Z
authority: ChiefArchitect
reviewer: chief-architect
reviewed_at: 2026-08-29T21:30:00Z
base_revision: 612f476c32fa7b1fbd38e4dc9f4c689d05b72191
decision: approved
scope:
  - python/ferrum/queryset.py
  - python/ferrum/expressions.py
  - crates/ferrum-sql/src/emit.rs
  - crates/ferrum-core/src/ir/mod.rs
---

# Named Authority Verdict

## Authority

ChiefArchitect

## Claims reviewed

1. `F`, `Star`, `Combinable`, `resolve_field_name` are added to
   `expressions.py` and compile through existing IR v4 nodes (no new IR nodes).
2. Query-plan safety guards (depth limit, duplicate detection, JOIN cap)
   enforce deterministic limits preventing N+1 and unbounded materialization.
3. W1-A/W1-B features (filter(None)/is_null, cast matrix, write-scope guards,
   select_for_update, is_write) are preserved.
4. IR version is NOT bumped (remains 4); no new IR nodes introduced.
5. 8 blockers (B1-B8) are correctly identified and escalated for IR changes
   requiring ChiefArchitect approval.

## Evidence

### F / Star / Combinable architecture

`git diff HEAD -- python/ferrum/expressions.py` (+89 lines):

- `Combinable` base class: documents that subclasses lower through existing
  IR and never carry raw SQL fragments. ✅
- `F(Combinable)`: typed field reference with `__slots__`, `__repr__`,
  `__eq__`, `__hash__`. Constructor validates non-empty `str`. Does NOT add
  comparison operators (field-to-field comparison documented as blocker).
  ✅
- `Star(Combinable)`: marker for `COUNT(*)`. `Aggregate.count(Star())` and
  `Aggregate.count()` produce identical SQL. ✅
- `resolve_field_name(value: str | F) -> str`: returns field name from `F` or
  `str`; raises `TypeError` for any other type. Never accepts raw SQL. ✅

These compile through existing IR v4 nodes — `F.name` is resolved to a plain
string field name before being passed to `group_by`/`date_trunc`/`order_by`/
`Aggregate` factories, which use the existing metadata-allowlist validation.
No new IR nodes. ✅

### Query-plan safety guards

`git diff HEAD -- python/ferrum/queryset.py`:

- `_MAX_SELECT_RELATED_DEPTH: int = 5` (queryset.py:83): caps
  `select_related()` depth. ✅
- `_MAX_TOTAL_JOINS: int = 12` (queryset.py:85): caps total JOINs in
  `_build_ir()`. ✅
- Duplicate detection in `select_related()` (queryset.py:1654-1660): rejects
  `name in existing` with `FerrumCompileError` "Duplicate relation". ✅
- Depth check in `select_related()` (queryset.py:1668-1674): rejects
  `new_depth > _MAX_SELECT_RELATED_DEPTH`. ✅
- Total JOIN cap in `_build_ir()` (queryset.py:1094-1100): rejects
  `len(joins) > _MAX_TOTAL_JOINS`. ✅

These are conservative deterministic limits consistent with AGENTS.md §7
("Prevent accidental N+1 and unbounded materialization"). ✅

### IR version / IR nodes

```
python/ferrum/queryset.py:65: _IR_VERSION: int = 4
crates/ferrum-core/src/ir/mod.rs:18: pub const IR_VERSION: u32 = 4;
```

`git diff HEAD -- crates/ferrum-core/src/ir/mod.rs crates/ferrum-core/src/compile/mod.rs crates/ferrum-sql/src/lib.rs`: empty. No IR schema, compiler, or
lib changes. ✅

### W1-A/W1-B preservation

- `filter(None)` / `is_null` mapping: preserved (queryset.py:453-463). ✅
- Cast matrix in `emit.rs`: unchanged except 3 new FieldType arms
  (`Domain` merged into `Text|Enum`, `Citext`, `Inet`). ✅
- `select_for_update()`: preserved (queryset.py:1789). ✅
- `_check_write_scope()`: preserved (queryset.py:3219, 3291, 3422, 3487). ✅
- `is_write=True` on write terminals: preserved (queryset.py:2340, 2710,
  3091, 3193, 3510). ✅

### Blocker assessment (B1-B8)

Inspected `crates/ferrum-core/src/ir/mod.rs`:

- **B1** (multi-hop): `JoinSpec` (line 170) has `local_field: FieldRef`
  referencing base model only. No `from_alias` field. Confirmed blocker. ✅
- **B2** (WHERE EXISTS): `Predicate` enum (line 314) has only `And`, `Or`,
  `Not`, `Filter`. No `Exists` variant. Confirmed blocker. ✅
- **B3** (scalar subqueries): No `ScalarSubquery` type. Confirmed. ✅
- **B4** (CASE WHEN): No `Case`/`When` types. Confirmed. ✅
- **B5** (database functions): No function-call expression type. Confirmed. ✅
- **B6** (window functions): No `Window`/`Over`/`PartitionBy`. Confirmed. ✅
- **B7** (CTEs): No `With`/`CTE` types. Confirmed. ✅
- **B8** (UNION): No `SetOp`/`Union`/`Intersect`/`Except`. Confirmed. ✅

The executor correctly identified all 8 as IR changes requiring
ChiefArchitect escalation and did NOT implement them. This respects the
§2/§4 boundary and the task contract's "Do NOT bump IR version or introduce
new IR nodes without escalating to the coordinator" instruction. ✅

### Boundary compliance (AGENTS.md §2, §4)

- §2.1 Python owns public ergonomics: `F`/`Star`/`Combinable` live in
  Python (`expressions.py`). ✅
- §2.2 Rust owns performance-critical internals only: no Rust IR/compiler
  changes; the only Rust change is `emit.rs` cast-matrix arm merging (a
  pre-existing build fix from W2-A's `metadata.rs` FieldType variants). ✅
- §2.9 No raw SQL escape hatches: no `raw()`/`extra()`/string fragments. ✅
- §2.10 No per-request mutable shared state in Rust: no Rust state changes. ✅

## Findings

No blocking findings. The architecture is sound:

- Expression types compile through existing IR v4 nodes without new IR nodes.
- Safety guards are conservative deterministic limits aligned with §7.
- IR version is unchanged; 8 blockers correctly escalated.
- W1-A/W1-B features preserved.
- PyO3 boundary respected (no Rust IR/compiler changes).

## Decision

`approved`

This record grants only the ChiefArchitect gate. It does not substitute for
another authority or independent verification.
