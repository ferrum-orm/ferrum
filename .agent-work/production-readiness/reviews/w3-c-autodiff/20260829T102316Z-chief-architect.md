---
task_id: w3-c-autodiff
run_id: 20260829T102316Z
authority: ChiefArchitect
reviewer: production-readiness-orchestration
reviewed_at: 2026-08-29T11:30:00Z
base_revision: 22931420c7c7212fe4e9718faa710d9a890ea473
decision: approved
scope:
  - python/ferrum/migrations/autodiff.py
  - tests/python/unit/test_autodiff.py
  - tests/python/integration/test_autodiff.py
---

# Named Authority Verdict

## Authority

ChiefArchitect

## Claims reviewed

1. Autodiff architecture extends the base `compute_plan` without modifying
   orchestrator.py/operations.py/base.py (import-only, AGENTS.md §6, §7).
2. Type change detection: emits `alter_column` with `sql_type` set; always
   classified destructive per W1-C — never guesses a "safe" widening.
3. Rename detection: requires explicit `rename_hints`; never guesses ambiguous
   renames; raises `FerrumMigrationError` before any SQL is emitted.
4. Constraint/relation detection: FK add/drop on existing tables (base
   `compute_plan` only handles new tables).
5. Index attribute detection: drop + re-add when `unique`/`using`/`where`/
   `columns` changed on an existing index.
6. Extension/RLS/function detection: tracked in `AutodiffSchemaState` for
   round-trip replay; not model-declarable, so no autodiff-emit path
   (documented design decision).
7. Schema-state replay: `build_extended_state` replays ALL migration op kinds
   into `AutodiffSchemaState`; round-trip tests from empty schema through
   upgrade/revert.
8. Destructive classification: uses W1-C's `_is_op_destructive` directly for
   recompute; the apply path independently scans ops (defense in depth).
9. No raw SQL escape hatches (AGENTS.md §2.9): emits only op dicts consumed
   by `orchestrator._op_to_sql`.

## Evidence

### Source inspection — `python/ferrum/migrations/autodiff.py` (665 lines)

- `AutodiffSchemaState` (autodiff.py:100-120) subclasses `SchemaState`
  (not frozen, not modifying base). Adds `foreign_keys`, `extensions`,
  `rls_enabled`, `rls_forced`, `policies`, `functions`, `full_text_indexes`.
  Because `compute_plan` accepts `SchemaState` via `isinstance`, the base
  autodiff path runs unchanged when an extended state is passed. This is a
  clean extension that respects W1-C/W3-A ownership boundaries.

- `build_extended_state` (autodiff.py:153-183) replays all 19 migration op
  kinds into the extended state. `_apply_op_to_state` (autodiff.py:186-323)
  handles each kind explicitly; unknown kinds are silently skipped for
  forward-compatibility. Drop-table cascades to indexes, FKs, RLS, policies,
  FTS (autodiff.py:217-227) — correct cascade semantics for state
  reconstruction.

- `compute_autodiff_plan` (autodiff.py:331-665):
  - Calls base `compute_plan` first (autodiff.py:394), then post-processes.
    This is the correct extension pattern — the base path runs unchanged and
    the extended detection layers on top.
  - **Rename enforcement** (autodiff.py:422-450): `disappeared` columns without
    a matching hint raise `FerrumMigrationError` [FERR-M001]. Hint target not
    in `appeared` raises. Incompatible types raise. Spurious `add_column` ops
    from `compute_plan` are removed (autodiff.py:462-470). Never guesses.
  - **Type change** (autodiff.py:482-506): emits/merges `alter_column` with
    `sql_type` set. Merges into existing `alter_column` ops where possible to
    avoid duplicate ALTER TABLE. Case-insensitive comparison via
    `_normalize_type` (autodiff.py:132-136).
  - **FK add/drop** (autodiff.py:512-548): detects desired FKs from model
    relations; drops FKs no longer desired; adds new FKs. Only for existing
    tables (new tables handled by `compute_plan`).
  - **Index attribute changes** (autodiff.py:554-608): compares existing
    index state vs desired; drops + re-adds when attributes differ. Removes
    spurious `add_index` from `compute_plan` before appending drop + re-add.
  - **FTS add/drop** (autodiff.py:615-649): handles both new and existing
    tables (base `compute_plan` does not emit FTS ops at all).
  - **Destructive recompute** (autodiff.py:656-664): `has_destructive = any(
    _is_op_destructive(op) for op in ops)` — uses W1-C's function directly.
    The apply path (orchestrator.py MIG-2) independently scans ops and never
    trusts the plan flag.

### Ownership boundary verification

```
$ git diff --name-only 22931420c7c7212fe4e9718faa710d9a890ea473 -- python/ferrum/migrations/
(empty — orchestrator.py, operations.py, base.py NOT modified)
```

The module is import-only with respect to orchestrator.py, operations.py,
base.py, loader.py, tokens.py (autodiff.py:38-40 docstring). Confirmed.

### Architectural concerns addressed

1. **Subclass vs modify**: `AutodiffSchemaState` subclasses `SchemaState`
   rather than modifying it — respects W1-C/W3-A ownership and keeps the base
   path unchanged. Sound.

2. **Post-processing vs rewrite**: `compute_autodiff_plan` calls base
   `compute_plan` then post-processes, rather than reimplementing the base
   logic. This avoids duplication and keeps the base path as the single source
   for create_table/add_column/add_index/drop_index/alter_column
   (default/nullability). Sound.

3. **Extensions/RLS/functions not model-declarable** (autodiff.py:357-360):
   there is no model metadata to diff against, so autodiff cannot emit ops
   for them. State replay tracks them for round-trip tests. This is the
   correct YAGNI decision (AGENTS.md §7) — no speculative complexity for
   metadata that does not exist yet.

4. **Table drops not detected**: consistent with base `compute_plan` behavior
   (which also doesn't emit `drop_table` for removed models). A future
   iteration could add explicit table-drop detection with a confirmation gate.
   Documented as a risk/follow-up, not a defect.

5. **No raw SQL** (AGENTS.md §2.9): the module emits only op dicts consumed by
   `orchestrator._op_to_sql`. No string interpolation, no `.raw()`/`.extra()`.
   Identifiers come from model-metadata allowlists or prior migration ops.
   Compliant.

### Test evidence (from verification)

- 40 unit tests pass (`test_autodiff.py`).
- 5 integration tests pass on live PostgreSQL (`test_autodiff.py` integration).
- Subclass relationship verified: `AutodiffSchemaState` IS-A `SchemaState`,
  accepted by base `compute_plan` via `isinstance`.
- Round-trip: empty schema → upgrade → revert verified on live PostgreSQL.

## Findings

1. **[INFO] Extensions/RLS/functions not model-declarable**: autodiff cannot
   emit ops for extensions, RLS, or functions (no model metadata to diff
   against). State replay tracks them for round-trip tests. Documented design
   decision (autodiff.py:357-360). Not a defect — correct YAGNI application.
   - Correction: none required.

2. **[INFO] Table drops not detected**: autodiff does not emit `drop_table`
   for removed models (consistent with base `compute_plan`). Future iteration
   could add explicit table-drop detection with a confirmation gate.
   - Correction: none required for W3-C scope.

3. **[INFO] Rename hints are function arguments, not model-declarable**: a
   future `Meta.rename_hints` declarative form could improve developer
   ergonomics. This is a product decision for ProductManager.
   - Correction: none required for W3-C scope.

## Decision

**approved**

The autodiff architecture is sound:
- Extends the base `compute_plan` via subclassing and post-processing — no
  modification to orchestrator.py/operations.py/base.py.
- Type changes always destructive (never guesses safe widening) — consistent
  with W1-C.
- Rename detection requires explicit hints, never guesses — raises before any
  SQL is emitted.
- FK/index/FTS detection covers the gaps in base `compute_plan`.
- Schema-state replay handles all 19 migration op kinds for round-trip tests.
- No raw SQL escape hatches (AGENTS.md §2.9).
- Destructive classification uses W1-C's `_is_op_destructive` directly;
  apply path independently scans ops (defense in depth).

This record grants only the ChiefArchitect gate.
