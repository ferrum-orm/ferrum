---
task_id: w3-a-migration-graph
run_id: 20260829T093132Z
authority: SecurityEngineer
reviewer: security-engineer
reviewed_at: 2026-08-29T11:05:00Z
base_revision: 87f39966d60303b30943308c9123418d9d47252e
decision: approved
scope:
  - python/ferrum/migrations/orchestrator.py
  - python/ferrum/migrations/loader.py
  - python/ferrum/migrations/base.py
---

# Named Authority Verdict

## Authority

SecurityEngineer

## Claims reviewed

1. Migration-apply DDL classification is preserved: `_is_op_destructive` and
   `operations.AlterColumn.classification` are untouched by W3-A.
2. Destructive/type-narrowing classification is correct: `AlterColumn` with
   `sql_type is not None` (type narrowing) OR `not_null is True` (SET NOT NULL)
   is classified `"destructive"`; the W1-C confirm gate (`apply()` line
   1371-1372) consults `_is_op_destructive` rather than trusting JSON
   `requires_confirmation`.
3. No automatic source-code execution from untrusted files:
   `run_data_migration` refuses `is_trusted=False` before any execution; the
   loader trust boundary (developer-authored migration files via
   `spec.loader.exec_module`) is unchanged.
4. Data migration transaction policy is safe: `"required"` wraps in
   `conn.transaction()` (rolls back on exception/cancellation; raises on
   thin-parity backends that cannot honor the requested atomicity);
   `"none"` runs in autocommit with documented partial-state risk; unknown
   policy rejected.
5. Offline SQL does not leak secrets: `generate_offline_sql` emits only DDL
   identifiers (from model-metadata allowlists) and a content digest; no DSN,
   password, bound value, or row data appears in the `OfflineSqlPlan`.

## Evidence

### DDL classification preserved (fresh source inspection)

`git diff HEAD -- python/ferrum/migrations/operations.py` → 0 lines
(unchanged). `AlterColumn.classification` (operations.py:215-221):
```python
if self.not_null is True or self.sql_type is not None:
    return "destructive"
return "safe"
```
This closes the W0-A §5a type-narrowing confirm hole (W1-C owns; W3-A
preserves). `_is_op_destructive` (orchestrator.py:156-170) mirrors this:
```python
if kind == "alter_column":
    return op.get("not_null") is True or op.get("sql_type") is not None
```
The `apply()` confirm gate (orchestrator.py:1371-1372) uses
`_is_op_destructive`, not the JSON `requires_confirmation` flag:
```python
is_destructive = any(_is_op_destructive(op) for op in ops)
if (is_destructive or plan.get("requires_confirmation")) and not confirm:
```
`OfflineSqlMigration.has_destructive` (orchestrator.py:2099) reuses
`_is_op_destructive` — no parallel classification. W3-A adds no new
classification logic.

### No automatic source-code execution from untrusted files

`run_data_migration` (orchestrator.py:1986) checks `is_trusted` before any
execution:
```python
if not getattr(migration, "is_trusted", False):
    raise FerrumMigrationError(
        "Refusing to run untrusted data migration. Data migrations must be "
        "developer-authored subclasses of DataMigration with is_trusted=True. ..."
    )
```
`DataMigration.is_trusted: ClassVar[bool] = True` (orchestrator.py:1968).
Subclasses inherit `True`; only an explicit override to `False` triggers the
refusal. The loader trust boundary is unchanged: `loader.scan` imports
developer-authored migration files via `spec.loader.exec_module` (existing
W1-C behavior; W3-A adds no new import path). Verified by
`test_untrusted_refused_before_run` (unit) — `run_data_migration` raises
matching `"untrusted"` before calling `.run()`.

### Data migration transaction policy safe

`run_data_migration` validates policy against
`_DATA_MIGRATION_POLICIES = frozenset({"required","none"})` before dispatch.
`"required"` opens `conn.transaction()` (orchestrator.py:2010) which raises
on thin-parity backends lacking transaction support — the runner does not
silently degrade atomicity. Failure inside `"required"` re-raises as
`FerrumMigrationError` carrying `type(exc).__name__` only (no message,
bound values, or DSN). `"none"` documents partial-state risk in the wrapped
error. Verified by `test_data_migration_required_policy_commits`,
`test_data_migration_required_policy_rolls_back_on_error` (integration: table
not created on failure),
`test_data_migration_none_policy_runs_in_autocommit` (integration).

### Offline SQL does not leak secrets

`generate_offline_sql` (orchestrator.py:2080) takes `modules` and `dialect`
— no `Connection`, no DSN. It reads module file content, computes
`compute_digest(name, content)`, renders SQL via `_op_to_sql(op, dialect=...)`.
`_op_to_sql` (orchestrator.py:478) emits only DDL identifiers quoted via
`_quote_ident` (from model-metadata allowlists per AGENTS.md §2.9); no bound
parameter values are emitted into the offline bundle. The `OfflineSqlPlan`
dataclass contains `name`, `digest`, `reversible`, `has_destructive`,
`phases` (phase, kind, table, sql) — no credential, DSN, or row-data fields.
`recovery_guidance` (orchestrator.py:1862) outputs migration names and states
only. Verified by `test_graph_recovery_guidance_checksum_mismatch`
(integration): the hint contains no file content, DSN, or bound value.

### Redaction of error surfaces

All W3-A error paths raise `FerrumMigrationError` with structured, sanitized
messages: migration names, `type(exc).__name__`, and stable `[FERR-Mxxx]`
codes. No `exc` message, `DETAIL`/`HINT`, bound value, or DSN is forwarded.
This satisfies AGENTS.md §3 (credential handling, error boundaries).

## Findings

| # | Severity | Evidence | Required correction |
|---|----------|----------|---------------------|
| 1 | info | `DataMigration` with `"required"` policy opens its own `conn.transaction()` rather than sharing the W1-C advisory-locked apply transaction. A data migration that mutates rows touched by the DDL in the same migration file would not be atomic with the DDL apply. | None for W3-A. The transaction policy is explicit and safe per the contract. Sharing the W1-C apply tx requires CLI integration (W1-C/W2-F owned) and is out of scope. Documented as a follow-up risk. |
| 2 | info | `"none"` policy runs in autocommit; a mid-flight failure leaves partial state. The wrapped error documents this risk. | None. The behavior is explicit, documented in the `DataMigration` docstring and the error message, and matches the task contract. |

## Decision

`approved`

Migration-apply DDL classification is preserved and correct (type narrowing
+ SET NOT NULL are destructive; the confirm gate consults
`_is_op_destructive`, not JSON). No automatic source-code execution from
untrusted files — `is_trusted=False` is refused before execution and the
loader trust boundary is unchanged. Data migration transaction policy is
safe and explicit. Offline SQL emits only DDL identifiers and a content
digest; no secrets, DSNs, bound values, or row data leak. All AGENTS.md §3
security rules (SQL safety, credential handling, tiered observability, error
boundaries, migration safety) are preserved. This record grants only the
SecurityEngineer gate.
