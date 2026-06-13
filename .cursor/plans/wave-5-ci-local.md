# Wave 5 — Local CI Run Results

**Date:** 2026-06-13  
**Repo:** `/Users/guyshaked/Desktop/dev/guy/playground/ferrum`

## Summary Table

| Step | Task | Exit Code | Result |
|------|------|-----------|--------|
| 1 | `mise run lint-python` (ruff check + format) | 0 | ✅ Pass |
| 2 | `mise run type-python` (ty check) | 0 | ✅ Pass |
| 3 | `cargo test -p ferrum-core -p ferrum-sql -p ferrum-migrate` | 0 | ✅ Pass |
| 4 | `mise run test-python-unit` (pytest unit + property) | 0 | ✅ Pass |
| 5 | `mise run import-boundary` (import-linter) | 0 | ✅ Pass |
| 6 | `cargo clippy --workspace -- -D warnings` | 0 | ✅ Pass |

**All 6 CI steps passed. No failures.**

---

## Step-by-step Output

### Step 1 — `mise run lint-python`

```
[lint-python] $ ruff check python/ tests/
warning: The following rules have been removed and ignoring them has no effect:
    - ANN101
    - ANN102
All checks passed!
[lint-python] $ ruff format --check python/ tests/
warning: The following rules have been removed and ignoring them has no effect:
    - ANN101
    - ANN102
41 files already formatted
```

> Exit code: 0. Two harmless warnings about removed rule codes (ANN101/ANN102) in pyproject.toml — not failures.

---

### Step 2 — `mise run type-python`

```
[type-python] $ ty check python/ferrum
All checks passed!
```

> Exit code: 0.

---

### Step 3 — `cargo test -p ferrum-core -p ferrum-sql -p ferrum-migrate`

```
test emit::tests::emit_select_limit_offset_are_bound_params ... ok
test emit::tests::emit_select_with_filter_uses_placeholder ... ok
test emit::tests::emit_insert_uses_placeholder_for_values ... ok
test emit::tests::emit_insert_returning_contains_all_fields ... ok
test emit::tests::emit_update_with_filter_uses_placeholders ... ok
test emit::tests::fingerprint_is_stable_for_same_shape ... ok
test emit::tests::rejects_invalid_sort_direction ... ok
test emit::tests::rejects_unknown_select_field_index ... ok
test emit::tests::rejects_unsupported_operator ... ok

test result: ok. 17 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out
```

> Exit code: 0. 17 Rust tests passing across ferrum-core and ferrum-sql (ferrum-migrate has no unit tests yet, only doc-tests).

---

### Step 4 — `mise run test-python-unit`

```
207 passed, 1 skipped in 0.33s
```

> Exit code: 0. 207 Python unit tests passing. 1 skipped (`test_property_placeholder` — intentional property test placeholder).

Coverage breakdown:
- `test_boundary.py` — PyO3 error/panic boundary (5 tests)
- `test_errors.py` — error taxonomy (9 tests)
- `test_hooks.py` — Tier A observability enforcement (11 tests)
- `test_init_scaffold.py` — CLI scaffold (5 tests)
- `test_ir_roundtrip.py` — IR JSON contract (11 tests)
- `test_migration_gates.py` — destructive/env gates (9 tests)
- `test_migrations.py` — orchestrator unit (20 tests)
- `test_model_metadata.py` — Pydantic metadata derivation (17 tests)
- `test_queryset_guards.py` — danger API guards (9 tests)
- `test_queryset_ir.py` — IR encoding and allowlist rejection (42 tests)
- `test_queryset_terminals.py` — terminal ops, decode, hooks (37 tests)

---

### Step 5 — `mise run import-boundary`

> Note: The task is named `import-boundary` in `mise.toml`, not `lint-imports`. The command
> `uv run lint-imports` runs the `import-linter` CLI.

```
Analyzed 41 files, 81 dependencies.
Contracts: 0 kept, 0 broken.
```

> Exit code: 0. No import contracts defined yet (`.importlinter` has `root_packages` set but no
> contracts written). No violations.

---

### Step 6 — `cargo clippy --workspace -- -D warnings`

```
Checking ferrum-core v0.1.0
Checking ferrum-sql v0.1.0
Checking ferrum-migrate v0.1.0
Checking ferrum-pyo3 v0.1.0
Finished `dev` profile [unoptimized + debuginfo] target(s) in 0.73s
```

> Exit code: 0. All 4 workspace crates pass clippy with `-D warnings` (zero warnings).

---

## Observations / Minor Notes

1. **`lint-imports` task does not exist** — the correct task name is `import-boundary`. The CI
   script called `mise run lint-imports` which returns exit 1; this was corrected to
   `mise run import-boundary` for this run.

2. **ANN101/ANN102 ruff warnings** — `pyproject.toml` references two removed ruff rule codes.
   These are advisory warnings only (not failures). They can be cleaned up from the
   `[tool.ruff.lint] ignore` list at any time without functional impact.

3. **Import contracts are empty** — `.importlinter` is configured with `root_packages` but no
   contracts yet. The import-boundary gate is a no-op until contracts are authored as part of
   ADR-006 (centralized error/hook layer) or later boundary-hardening work.

4. **Integration tests not run** — require a live PostgreSQL connection (`FERRUM_TEST_DATABASE_URL`).

5. **`maturin develop` not run** — the native extension was already compiled in a prior session;
   all Python tests that exercise the PyO3 boundary passed against the cached `.so`.

---

## Status

**Local CI: GREEN** — all gated checks pass as of 2026-06-13.
