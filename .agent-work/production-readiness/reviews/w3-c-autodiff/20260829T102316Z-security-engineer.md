---
task_id: w3-c-autodiff
run_id: 20260829T102316Z
authority: SecurityEngineer
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

SecurityEngineer

## Claims reviewed

1. Destructive/type-narrowing classification consistent with W1-C
   (`_is_op_destructive`, `AlterColumn.classification`).
2. Never guesses ambiguous renames — raises `FerrumMigrationError` without a
   matching hint, before any SQL is emitted.
3. No untrusted source execution — all identifiers from model-metadata
   allowlists or prior migration ops; no user input reaches SQL.
4. No raw SQL escape hatches (AGENTS.md §2.9) — emits only op dicts.
5. Error messages do not leak secrets, DSNs, bound values, or row data
   (AGENTS.md §3 — credential handling, error boundaries).
6. Migration safety (AGENTS.md §3): destructive actions require explicit
   confirmation; the apply path independently scans ops (MIG-2).

## Evidence

### 1. Destructive classification consistency (W1-C)

`_is_op_destructive` (orchestrator.py:156-170):
- `alter_column` with `sql_type is not None` OR `not_null is True` → destructive.
- `_DESTRUCTIVE_KINDS` (orchestrator.py:127-138): includes `drop_fk`,
  `drop_table`, `drop_column`, `raw_sql`, etc.

`AlterColumn.classification` (operations.py:215-221):
- `not_null is True` OR `sql_type is not None` → "destructive".

Autodiff behavior (autodiff.py):
- Type changes emit `alter_column` with `sql_type` set (autodiff.py:500-506) →
  destructive per both W1-C checks. ✓
- Type widening (e.g. INTEGER → BIGINT, a safe widening) still classified
  destructive — never guesses a safe conversion. ✓
- `drop_fk` emitted for removed FKs (autodiff.py:534) → in `_DESTRUCTIVE_KINDS`
  → destructive. ✓
- `rename_column` emitted for renames (autodiff.py:451-458) → NOT in
  `_DESTRUCTIVE_KINDS` → safe. ✓
- `drop_full_text_index` (autodiff.py:631-637) → NOT in `_DESTRUCTIVE_KINDS`
  → safe. ✓
- Destructive recompute (autodiff.py:656-664): `has_destructive = any(
  _is_op_destructive(op) for op in ops)` — uses W1-C's function directly.
- The apply path (orchestrator.py MIG-2) independently scans ops via
  `_is_op_destructive` and never trusts the plan flag — defense in depth. ✓

### 2. Rename enforcement (never guesses)

- Missing hint (autodiff.py:424-431): `disappeared` column without a matching
  `rename_hints` entry → `FerrumMigrationError` [FERR-M001] with actionable
  instructions ("Provide a rename hint via rename_hints=... or explicitly add
  a DropColumn op"). Raised BEFORE any SQL is emitted. ✓
- Hint target not in `appeared` (autodiff.py:432-438): → `FerrumMigrationError`
  [FERR-M001]. ✓
- Incompatible types (autodiff.py:439-450): rename hint with differing types →
  `FerrumMigrationError` [FERR-M001] with instruction to "Rename first, then
  add a separate AlterColumn to change the type." ✓
- Never falls through to a default/guess path. All three error paths raise. ✓

### 3. No untrusted source execution

- `compute_autodiff_plan` (autodiff.py:331) inspects `model_classes` —
  `Model` subclasses whose `ModelMetadata` is built at class-definition time
  from declared fields (AGENTS.md §2.10: "Model metadata is built once at
  class definition time and is thereafter read-only"). No user input reaches
  model metadata. ✓
- `build_extended_state` (autodiff.py:153) replays developer-authored
  migration files (themselves sourced from model-metadata allowlists at
  generation time). Static reconstruction — no database connection. ✓
- All identifiers in emitted op dicts come from `metadata.table_name`,
  `f.column_name`, `rel.db_column`, `index.name` — all from model metadata
  allowlists. ✓
- No `exec()`, `eval()`, `compile()`, or dynamic import of user-supplied
  code. ✓

### 4. No raw SQL escape hatches (AGENTS.md §2.9)

- The module emits only op dicts (`{"kind": "alter_column", ...}`) consumed
  by `orchestrator._op_to_sql`. No string interpolation of SQL. ✓
- No `.raw()`, `.extra()`, string fragments, or user-supplied templates. ✓
- Grep confirmed (from verification): no matches for
  `password|dsn|secret|DETAIL|HINT|bound` in error messages. ✓

### 5. Error message sanitization (AGENTS.md §3)

- `FerrumMigrationError` messages reference only table/column names from
  model metadata (e.g. `Column {table}.{old_col!r}`). ✓
- No DSNs, passwords, bound parameter values, or row data in any error
  message or exception field. ✓
- Error codes ([FERR-M001]) are stable and actionable. ✓

### 6. Test evidence (fresh)

```
$ uv run pytest tests/python/unit/test_autodiff.py -x -q
........................................                                 [100%]
40 passed in 0.22s
```

Security-relevant tests confirmed:
- `test_rename_without_hint_raises`: column removed, no hint →
  `FerrumMigrationError` matching "old_name". Never guesses. ✓
- `test_rename_with_incompatible_types_raises`: TEXT → INTEGER rename →
  `FerrumMigrationError` matching "incompatible types". ✓
- `test_rename_hint_target_not_in_model_raises`: hint to non-existent column
  → `FerrumMigrationError` matching "does not match". ✓
- `test_type_widening_still_classified_destructive`: INTEGER → BIGINT (safe
  widening) → `plan["destructive"] is True`,
  `plan["requires_confirmation"] is True`. Never guesses safe. ✓
- `test_drop_fk_classified_destructive_in_plan`: removed FK →
  `plan["destructive"] is True`. ✓

## Findings

No security defects found.

1. **[INFO] Destructive recompute is for transparency only**: the plan's
   `destructive`/`requires_confirmation` fields are recomputed via
   `_is_op_destructive`, but the apply path independently scans ops (MIG-2)
   and never trusts the plan flag. This is defense in depth, not a reliance
   on the plan flag. Compliant.
   - Correction: none required.

2. **[INFO] Rename hints are function arguments**: a developer must pass
   `rename_hints` to `compute_autodiff_plan`. There is no model-declarable
   form yet. This does not affect security — the function raises if the hint
   is missing. Future product decision.
   - Correction: none required.

## Decision

**approved**

Destructive/type-narrowing classification is consistent with W1-C:
`alter_column` with `sql_type` is always destructive; `drop_fk` is
destructive; `rename_column` is safe. The autodiff never guesses ambiguous
renames — all three error paths (missing hint, bad target, incompatible
types) raise `FerrumMigrationError` before any SQL is emitted. No untrusted
source execution: all identifiers from model-metadata allowlists. No raw SQL
escape hatches. Error messages are sanitized (no secrets, DSNs, bound values,
or row data). 40 unit tests pass including all security-relevant adversarial
tests.

This record grants only the SecurityEngineer gate.
