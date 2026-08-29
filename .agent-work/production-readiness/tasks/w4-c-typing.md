---
task_id: w4-c-typing
wave: wave-4
owner: production-readiness-executor
status: in_progress
run_id: 20260829T093132Z
shared_path_lease: null
dependencies:
  - w1-a-query-correctness
  - w1-d-error-taxonomy
owned_paths:
  - python/ferrum/_native.pyi
  - python/ferrum/py.typed
  - tests/python/unit/test_typing_contract.py
security_triage_complete: true
security_surfaces:
  sql_compilation: false
  migration_apply: false
  errors_redaction: false
  auth_secrets: false
  rls_admin_gucs: false
  schema_selection: false
security_review: false
security_review_justification: Typing stubs and IDE contract; no security surfaces
architecture_review: false
product_review: false
code_review: true
---

# Task: Typing and IDE contract

## Specify

### Problem

Strict `ty`, Pyright/Pylance, and PyCharm behavior needs verification for Model,
QuerySet[T], projections, aggregates, relations, codecs, transactions, and
contrib adapters. Dynamic surfaces may need replacement with explicit generic
protocols/stubs. `py.typed` and `_native.pyi` must be synchronized.

### Scope

`python/ferrum/_native.pyi` (PyO3 native stub), `python/ferrum/py.typed` (PEP 561
marker), and owned typing-contract tests. Add compile-time fixture tests for
accepted and rejected API use.

### Non-goals

No source code edits to non-stub files — this workstream only writes stubs and
typing tests. No `__init__.py` edits (shared path, no lease). No `README.md` /
`CHANGELOG.md` (record bullets in log). If a typing issue requires a source code
fix, record it as a follow-up blocker — do NOT edit non-owned source files.

### Invariants and failure modes

Strict typing for Model, QuerySet[T], projections, aggregates, relations, codecs,
transactions, and contrib adapters. `py.typed` and `_native.pyi` synchronized.
Compile-time fixture tests for accepted and rejected API use. No dynamic
`__getattr__` / monkey-patching that defeats type checkers.

### Acceptance criteria

- `ty` passes on the full project (or owned paths — record pre-existing failures
  in unowned files as known blockers).
- Pyright/Pylance behavior verified for core types.
- `_native.pyi` synchronized with the actual PyO3 extension.
- `py.typed` present and correct.
- Compile-time fixture tests for accepted and rejected API use.
- IDE-friendly: full type annotations, explicit class/method surfaces, no dynamic
  `__getattr__` / monkey-patching.

## Plan

Audit `_native.pyi` and `py.typed` for gaps. Update stubs to match current PyO3
extension. Add typing fixture tests. CodeReviewer required. ChiefArchitect and
SecurityEngineer not required.

## Tasks

1. Audit `_native.pyi` and `py.typed` and identify gaps vs current PyO3 extension.
2. Update `_native.pyi` to match the actual PyO3 extension surface.
3. Verify `py.typed` is present and correct.
4. Run `ty` and record results (owned paths vs pre-existing failures in unowned).
5. Add compile-time fixture tests for accepted and rejected API use.
6. Verify Pyright/Pylance behavior (if available).
7. Focused checks plus `mise run ci-local`.

## Implement

Coordinator marked `in_progress` at `20260829T093132Z` with exclusive owned paths
and no shared-path lease. Implement the Tasks section.

## Validation contract

`ty check` on owned paths, fixture tests, then `mise run ci-local`. Record ty
output and any pre-existing failures in unowned files.

## Independent verification contract

Verifier proves stubs match PyO3 surface, `py.typed` present, fixture tests
pass, and no new ty errors in owned paths. Named gates: CodeReviewer.
ChiefArchitect `not_required`. SecurityEngineer `not_required`. ProductManager
`not_required`.

## Revert contract

Revert only owned stub/test files from this run. Preserve all other workstreams.
