# Wave 5 PM Acceptance Review

**Date:** 2026-06-13  
**Reviewer:** ProductManager  
**Overall Verdict:** Blocked — 4 gaps to close before release qual

## Must-have checklist

| # | Requirement | Status | Notes |
|---|-------------|--------|-------|
| 1 | `class User(ferrum.Model)` with Pydantic v2 field introspection | ✅ Met | `__pydantic_init_subclass__` hook wires metadata |
| 2 | QuerySet read path: `.all()`, `.filter()`, `.get()`, `.count()` | ✅ Met | Async, with conn parameter |
| 3 | QuerySet write path: `.create()`, `.filter().update()`, `.filter().delete()` | ✅ Met | All three terminals implemented |
| 4 | Danger API: unscoped `delete()`/`update()` require `danger_*` method | ✅ Met | Python guard + Rust defense-in-depth (danger flag added) |
| 5 | Async-only API | ✅ Met | No sync wrappers exported |
| 6 | `ferrum.connect(dsn)` + `FERRUM_DATABASE_URL` env var | ✅ Met | Connection pool with redacted diagnostics |
| 7 | Observability: `ferrum.register_hook` + Tier A payload | ✅ Met | Exported from top-level namespace (fixed Wave 5) |
| 8 | Migration dry-run: CLI + `ferrum.migrations.apply(dry_run=True)` | ⚠️ Partial | `apply()` works; `compute_plan()` (schema-diff) is a stub |
| 9 | Migration confirmation gate: destructive ops blocked without `--confirm` | ✅ Met | MIG-2/MIG-3 gates implemented and test-covered |
| 10 | Error taxonomy: `FerrumError` base + typed subclasses exported | ✅ Met | All error classes exported from `ferrum` namespace |

## Remaining gaps

### Gap 1 (HIGH): `compute_plan()` not implemented
`ferrum migrations dry-run` and `ferrum.migrations.compute_plan()` raise `NotImplementedError`. The PRD migration flow requires auto-generating a plan from model diffs. Without this, users must hand-author plan JSON.

### Gap 2 (HIGH): Token confirmation not wired
`gates.py`/`tokens.py` exist but are not called from `orchestrator.apply()`. The stale/mismatched-token rejection criterion from the PRD is unenforceable.

### Gap 3 (MEDIUM — FIXED this wave): `danger_delete_all()`/`danger_update_all()` were `NotImplementedError`
Fixed: both methods are now implemented with `danger: true` IR flag bypassing Rust's MissingFilter check.

### Gap 4 (LOW — FIXED this wave): `ferrum.register_hook` not on top-level namespace
Fixed: `register_hook` and `clear_hooks` now exported from `ferrum.__init__`.

### Bonus: README signature mismatch
`User.objects.all()` in README should be `User.objects.all(conn)`.

## Gate decision

**Blocked.** Gaps 1 and 2 must be resolved. Gap 1 (schema-diff) is the most customer-visible. Gap 2 (token wiring) is a security-completeness issue.
