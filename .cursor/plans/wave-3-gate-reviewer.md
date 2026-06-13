# Wave 3 Gate — CodeReviewer

**Date:** 2026-06-13  
**Reviewer:** CodeReviewer (Ferrum)  
**Scope:** write path (`create`, `update`, `delete`), IR builders, hook wiring, error propagation,
row-count parsing, danger API, and companion unit tests.

---

## Verdict

**Approved with follow-ups — Wave 4 blocked on B-1 and B-2.**

The architectural invariants are intact: SQL identifiers flow only from model-metadata
allowlists, bound values are structurally out-of-band, the Tier A hook payload is
non-bypassable, and the danger-API filter guard fires before any I/O. Two issues must be
fixed before Wave 4 begins; four non-blocking warnings should be tracked.

---

## Review by Checklist Item

### 1. IR encoding (field refs and bind values)

**Pass.**

`_build_insert_ir` and `_build_update_ir` both emit:

```python
[{"index": field_index[name], "name": name}, _encode_bind_value(value)]
```

This is a 2-element JSON array, which serde deserializes correctly into Rust
`(FieldRef, BindValue)` tuples (serde encodes tuple structs as arrays). The adjacent-tagged
`BindValue` format (`{"type": "...", "value": ...}`) matches `#[serde(tag = "type", content =
"value")]` in `crates/ferrum-core/src/ir/mod.rs`.

`_build_delete_ir` delegates to `_build_ir()` for filter encoding; field-ref shape is
identical. All three IR paths are correct.

### 2. Danger API guard ordering

**Mostly pass — one ordering defect (W-1).**

The filter guard fires correctly and fires first:

```python
# delete() and update() — filter guard is the very first check
if not self._is_filtered:
    raise FerrumDangerApiError(...)
```

So `delete(conn=None)` and `update(conn=None)` without a filter both raise
`FerrumDangerApiError` immediately, before any compilation or I/O. ✓

However, when a filter *is* set but `conn=None`, the current ordering is:

```
1. filter guard — passes (filtered)
2. _native_ext guard
3. _compile_ir(...)     ← CPU compilation runs here
4. if conn is None: raise FerrumConfigError    ← too late
```

The docstring claims "the filter guard fires before any connection or compilation work", but
compilation work actually runs before the `conn is None` check. This wastes CPU on every
`delete(conn=None)` / `update(conn=None)` call with a filter present. See W-1.

### 3. Hook wiring symmetry

**Pass — all three write paths are symmetric.**

`create`, `delete`, and `update` each dispatch:
- `query_start` — before the `t0 = time.monotonic()` and the `try` block
- `query_failure` — inside `except Exception` with `duration_ms` and `failure_category`
- `query_success` — after the `try` block with `duration_ms` and `row_count`

The wiring mirrors the `all()`/`count()` read paths exactly.

**Test coverage gap** — see B-2 below.

### 4. Error propagation

**Mostly pass — one open ADR-006 gap (W-3).**

- asyncpg exceptions inside the try block: all three methods call `map_db_error(exc, ...)` and
  re-raise the mapped Ferrum error. ✓
- `FerrumConfigError` (not `AttributeError`) when `_native_ext is None`: each write method
  guards with `if _native_ext is None: raise FerrumConfigError(...)` before any other work. ✓
- PyO3 panics are caught by `std::panic::catch_unwind` in `lib.rs` and surfaced as
  `FerrumInternalError`. ✓

Open gap (acknowledged, ADR-006): `_compile_ir()` calls `_native_ext.compile_query()` outside
the `try/except` block. If Rust emits `_native.FerrumCompileError` (a `RuntimeError`
subclass), it propagates as a raw `RuntimeError`, not a `ferrum.errors.FerrumCompileError`
(`FerrumError` subclass). The ADR-006 comment in `_compile()` acknowledges this; Python-side
allowlist checks should catch most cases, but the safety net is absent. Tracked as W-3.

### 5. Row-count parsing

**Fail — B-1 (blocking).**

```python
parts = result.split() if result else []
row_count = int(parts[1]) if len(parts) > 1 else 0
```

- `None` or empty string → `0`. ✓
- `"DELETE 3"` / `"UPDATE 3"` → `3`. ✓
- `"UPDATE OK"` or any other non-numeric second token → **raw `ValueError` escapes** the
  Ferrum error taxonomy without being caught.

`ValueError` is not caught anywhere in the call stack before reaching the caller. This breaks
AGENTS.md §3 ("database errors map to a stable, sanitized Ferrum taxonomy") and §8 ("errors
are sanitized and actionable"). Fix: wrap with `try/except ValueError` and fall back to 0 (or
raise `FerrumInternalError`).

### 6. ADR constraints

**Pass.**

- No mutable Rust state per-request: `compile_query` is a pure function over `(&str, &str)`.
  Compilation is `AssertUnwindSafe` over immutable string references. ✓
- No sync API: all write terminals are `async`. ✓
- No SQL string building in Python: `queryset.py` builds only IR dicts; `ferrum-sql` does all
  SQL emission. ✓
- `RETURNING` clause in `emit_update` is emitted but Python calls `pool.execute()`, which
  discards row data. See W-2 (bandwidth waste, not a correctness or security issue).

### 7. Test completeness

**Partial fail — B-2 (blocking).**

**Covered:**

| Item | Test | File |
|---|---|---|
| `create` raises `FerrumConfigError` without ext | `test_create_raises_ferrum_config_error` | `test_queryset_terminals.py` |
| `delete` with filter raises `FerrumConfigError` without ext | `test_delete_with_filter_raises_config_error` | `test_queryset_terminals.py` |
| `update` with filter raises `FerrumConfigError` without ext | `test_update_with_filter_raises_config_error` | `test_queryset_terminals.py` |
| Filter guard fires on `delete()` without filter | `test_delete_without_filter_raises` | `test_queryset_guards.py` |
| Filter guard fires on `update()` without filter | `test_update_without_filter_raises` | `test_queryset_guards.py` |
| `danger_delete_all` bypasses the guard | `test_danger_delete_all_does_not_raise_danger_api_error` | `test_queryset_guards.py` |
| `danger_update_all` bypasses the guard | `test_danger_update_all_does_not_raise_danger_api_error` | `test_queryset_guards.py` |

**Not covered (missing, blocking — checklist item 7c):**

There are **zero** unit tests for hook dispatch on the write path. No test verifies that:
- `create()` dispatches `query_start`, `query_success`, and `query_failure`.
- `update()` dispatches these three events.
- `delete()` dispatches these three events.

The hook *mechanism* is thoroughly tested in `test_hooks.py`, but the hook *wiring* in the
write terminals is untested. AGENTS.md §8 requires: "tests that cover the new/changed
behavior." Hook dispatch wiring is new behavior in Wave 3 and must have tests.

---

## Blocking Issues

### B-1 — `ValueError` escapes error taxonomy on unexpected asyncpg status string

**Files:** `python/ferrum/queryset.py` lines 568 and 649  
**Lens:** Blast Radius (any unexpected asyncpg status → uncaught exception)

```python
# Current — crashes on e.g. "UPDATE OK"
parts = result.split() if result else []
row_count = int(parts[1]) if len(parts) > 1 else 0
```

Required fix: wrap `int(parts[1])` in `try/except ValueError` and fall back to 0 or raise
`FerrumInternalError`. This applies identically to both `delete()` (line 568) and `update()`
(line 649).

### B-2 — No unit tests for hook dispatch on write paths

**Files:** `tests/python/unit/` — no test covers `create`/`update`/`delete` hook dispatch  
**Lens:** Definition of Done (AGENTS.md §8)

Required: tests that mock the hook registry and verify `query_start`, `query_success`, and
`query_failure` are dispatched — with the right `fingerprint`, `operation`, `model`, `table`,
`duration_ms`, and `row_count` keys — for all three write operations (create, update, delete).
The failure path (DB exception → `query_failure`) must also be covered.

---

## Non-blocking Follow-ups

### W-1 — `conn is None` check fires after compilation in `delete()`/`update()`

**Files:** `queryset.py` lines 539-543, 620-624

When a filter is set but `conn=None`, `_compile_ir()` runs first (CPU, Rust round-trip), then
`FerrumConfigError` is raised. This violates the docstring invariant and wastes compilation
work. Move the `conn is None` guard above the `_compile_ir` call — after the native ext check
but before calling `_build_delete_ir` / `_build_update_ir`.

### W-2 — `emit_update` emits `RETURNING` but Python uses `pool.execute()`, discarding rows

**Files:** `crates/ferrum-sql/src/emit.rs` line 222, `queryset.py` line 636

The Rust emitter always appends `RETURNING {all_fields}` for UPDATE. The Python side uses
`asyncpg.execute()`, which discards row data entirely. This sends all updated column values
over the wire and then throws them away. Either:
- (a) Drop the RETURNING clause from `emit_update` (simplest fix for Wave 4).
- (b) Use `pool.fetch()` in `update()` and return updated instances (a Wave 4 feature).

The `lib.rs` docstring already notes this routing ambiguity ("fetch for select/insert/update,
execute for delete or update-without-return").

### W-3 — `_native.FerrumCompileError` (RuntimeError) escapes as non-Ferrum error

**Files:** `queryset.py` `_compile_ir()`, `crates/ferrum-pyo3/src/lib.rs`

`_compile_ir()` is called outside the `try/except` block in `create`, `delete`, and `update`.
A `_native.FerrumCompileError` (inherits `RuntimeError`, not `FerrumError`) propagates to the
caller without remapping. The comment `# ADR-006:` in `_compile()` acknowledges this gap. ADR-006
resolution is required before Wave 4 for any path where Rust can reject a well-formed Python IR.
For now, Python-side allowlists catch the common cases; the net is missing for edge conditions.

### W-4 — `create()` with `conn=None` raises `AttributeError`, not `FerrumConfigError`

**Files:** `queryset.py` lines 474 (`pool = conn._require_pool()`)

`create()` is typed `conn: Connection` (not `Optional`), but if a caller passes `None` with the
native ext present, the code compiles successfully then hits `conn._require_pool()` →
`AttributeError: 'NoneType' has no attribute '_require_pool'`. This is outside the `try/except`
block and escapes the error taxonomy. Inconsistent with `delete()`/`update()`, which guard
`conn is None` explicitly (albeit too late — see W-1). Add `if conn is None: raise
FerrumConfigError(...)` before compilation, or keep the type strict and remove the `None`
acceptance from the tests using `type: ignore`.

---

## Security Flag

**No.** SecurityEngineer sign-off is not required for this batch. No changes touch the auth
path, secrets handling, credential redaction, or migration-apply logic. The SQL safety model is
correct (allowlist identifiers, out-of-band values, RETURNING carries no user input into hook
payloads). Tier A redaction is non-bypassable.

W-3 is an error-taxonomy gap, not a credential or SQL-injection risk.

---

## Gate Decision

| Item | Status |
|---|---|
| IR encoding (field refs, bind values) | ✅ Pass |
| Danger guard fires before I/O | ✅ Pass (filter guard) / ⚠️ W-1 (conn check ordering) |
| Hook wiring symmetric | ✅ Pass (implementation) / ❌ B-2 (no tests) |
| Error propagation | ✅ Pass / ⚠️ W-3 (ADR-006 gap) |
| Row-count parsing fallback | ❌ B-1 |
| ADR constraints | ✅ Pass |
| Test completeness | ⚠️ Partial — B-2 blocking |
| Security flag | ✅ Not required |

**Wave 4 is blocked until B-1 and B-2 are resolved.**

W-1, W-2, W-3, and W-4 should be tracked and addressed before any external release candidate.
