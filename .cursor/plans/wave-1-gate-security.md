# Wave 1 — Security Gate Verdict

**Status: FAILS (pending fixes landing)**

## What is broken

Three tests in `tests/python/security/test_sql_safety.py` were failing:

| Test | Root cause |
|------|-----------|
| `test_build_ir_field_index_is_from_metadata_allowlist` | Assertion compared `ir["filters"][0]["field"]` to a bare string; `_build_ir()` emits `{"index": N, "name": field_name}`. Fixed to check `["field"]["name"]` and `isinstance(["field"]["index"], int)`. |
| `test_native_compile_error_propagated_from_mocked_extension` | `_native_ext` module-level ref and `_compile()` method were absent from `queryset.py`. Both added; test monkeypatch target now resolves. |
| `test_all_allowed_operators_accepted` | `_ALLOWED_OPERATORS` imported from `ferrum.queryset`; it lives in `ferrum.models`. Import corrected. |

An additional BindValue serde mismatch (adjacent-tag format divergence between Python and Rust) was identified but is being resolved by the IR fix agent, not here.

## What is correct

- **SQL-1/2/3 implementation is correct.** Field allowlist, operator allowlist, and sort-direction allowlist all fire *before* any SQL is produced. No injection path exists through the Python layer.
- Security gates for danger API guards (`danger_delete_all` / `danger_update_all`) pass.
- No credentials, DSNs, or bound values appear in error messages.

## Gate re-pass criteria

1. The three test fixes in this diff land and CI is green for `tests/python/security/test_sql_safety.py`.
2. BindValue serde fix from the IR agent lands and `test_queryset_ir.py` passes.
