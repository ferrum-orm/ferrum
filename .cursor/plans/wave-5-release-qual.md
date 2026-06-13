# Wave 5 Release Qualification

**Date:** 2026-06-13  
**Reviewer:** SecurityEngineer + CodeReviewer  
**Overall Verdict:** BLOCKED — 3 critical/high findings

## Security findings

### CRITICAL-1 — Token gate orphaned from `apply()` [FIXING: gap-fix agent]
`check_destructive_gate()` / `validate_token()` in `gates.py`/`tokens.py` implemented but never called from `orchestrator.apply()`. MIG-6 entirely unenforced on live apply path.

### CRITICAL-2 — DDL injection via `sql_type` / `default` [FIXING: inline]
`_col_def()` concatenates `col["sql_type"]` and `col["default"]` raw into DDL SQL. No allowlist validation. The MIG-4 tests only cover identifier quoting, not value slots.

### HIGH-2 — `apply()` trusts `requires_confirmation` from input JSON [FIXING: inline]
`apply()` fires the destructive gate only if `plan.get("requires_confirmation")` is truthy. A crafted JSON with `"requires_confirmation": false` + `drop_table` ops bypasses the gate when `confirm=False`. Must independently scan ops.

### MEDIUM-1 — PyO3 `RuntimeError` not remapped (W-3, ADR-006) [FIXING: inline]
`_compile_ir()` does not wrap `RuntimeError` from `_native_ext.compile_query()` into `FerrumCompileError`. Callers catching `FerrumCompileError` silently miss Rust-sourced rejections.

## Passing gates
- Dry-run default ✅
- All FERR-XXXX codes present ✅
- Tier A hook redaction non-bypassable ✅
- CRED-1 credential leak tested ✅
- `FerrumDangerApiError` on unscoped delete/update ✅
- Token malformed-input rejection ✅
- All identifiers double-quoted ✅

## Gate decision
BLOCKED until CRITICAL-1, CRITICAL-2, HIGH-2, MEDIUM-1 are resolved.
