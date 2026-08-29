---
task_id: w3-a-migration-graph
run_id: 20260829T093132Z
authority: ChiefArchitect
reviewer: chief-architect
reviewed_at: 2026-08-29T11:00:00Z
base_revision: 87f39966d60303b30943308c9123418d9d47252e
decision: approved
scope:
  - python/ferrum/migrations/orchestrator.py
  - python/ferrum/migrations/loader.py
  - python/ferrum/migrations/base.py
---

# Named Authority Verdict

## Authority

ChiefArchitect

## Claims reviewed

1. The new `MigrationGraph` primitive implements topological ordering
   architecture via Kahn's algorithm with deterministic name-sorted tie-breaks.
2. Target upgrade/downgrade design: `upgrade_plan(target)` is inclusive of
   target; `downgrade_plan(target)` is exclusive of target and rejects
   irreversible migrations in the plan.
3. `DataMigration` callable design: explicit `transaction_policy`
   (`"required"` / `"none"`); `"required"` wraps in `conn.transaction()`,
   `"none"` runs in autocommit; unknown policy rejected.
4. Offline SQL generation architecture: `generate_offline_sql` produces a
   `OfflineSqlPlan` with per-migration digest (matches `ledger.compute_digest`)
   and `pre_tx` / `tx` / `post_tx` phase annotations derived from
   `_split_ops_by_phase`; no DB I/O.
5. W1-C advisory lock, transactional DDL, atomic ledger, destructive confirm,
   env gate, and token gate are preserved byte-identical — the graph layer does
   not grow its own apply/revert path.
6. ADR-004 ownership stays with W1-C: `MigrationGraph` is read-only and does
   not acquire connections for apply/revert.

## Evidence

### Diff inspection (fresh)

`git diff HEAD -- python/ferrum/migrations/orchestrator.py
python/ferrum/migrations/loader.py python/ferrum/migrations/base.py`:
699 lines added. Exactly two deletions, both import-line expansions
(`from typing import TYPE_CHECKING, Any` → adds `ClassVar`;
`from ferrum.connection import Connection` → adds `ConnectionLike` and
`MigrationModule` under `TYPE_CHECKING`). All W3-A code in `orchestrator.py`
is appended after `migration_op_failure_from` (line 1567+). No W1-C function
body changed.

`git diff HEAD -- python/ferrum/migrations/operations.py
python/ferrum/migrations/ledger.py python/ferrum/migrations/tokens.py
python/ferrum/cli/`: 0 lines — confirmed untouched.

### Topological ordering architecture

`loader.topological_sort` (loader.py:121) is a public wrapper over the
internal `_topo_sort` (Kahn's algorithm). The constructor of `MigrationGraph`
(orchestrator.py:1649) calls `topological_sort(modules)` and stores modules
in deterministic order. `detect_cycle` (loader.py:152) performs a read-only
Kahn's pass that returns cycle participants instead of raising. Both use
`sorted()` for tie-breaks, guaranteeing two runs over the same input produce
identical output. Verified by `TestTopologicalSort` (chain, diamond,
independent sorted, missing-dep raises, cycle raises) and `TestDetectCycle`
(two/three-node cycles, missing-dep returns None).

### Target upgrade/downgrade design

`upgrade_plan(target)` (orchestrator.py:1805): iterates topological order,
skips applied, appends pending, breaks after `target` (inclusive). When
target is already applied, returns `[]`. `downgrade_plan(target)`
(orchestrator.py:1830): builds applied-in-order, reverts the suffix after
`target` (exclusive) in reverse topo order, raises `FerrumMigrationError`
naming irreversible migrations. Verified by `TestMigrationGraphUpgradePlan`
(target inclusive, already-applied → empty, unknown target raises) and
`TestMigrationGraphDowngradePlan` (no-target → last applied, target exclusive,
irreversible raises, not-applied → empty) plus integration
`test_graph_upgrade_plan_filters_applied`,
`test_graph_downgrade_plan_returns_last_applied`,
`test_graph_downgrade_plan_irreversible_raises`.

### Data migration callable design

`DataMigration` (orchestrator.py:1961) declares `transaction_policy:
ClassVar[str] = "required"` and `is_trusted: ClassVar[bool] = True`.
`run_data_migration` (orchestrator.py:1986) checks `is_trusted` before
execution, validates policy against `_DATA_MIGRATION_POLICIES`
(`{"required","none"}`), wraps `"required"` in `conn.transaction()` (which
raises on thin-parity backends lacking tx support), runs `"none"` directly
on the connection. Both failure paths wrap into `FerrumMigrationError` with
`type(exc).__name__` only. Verified by `TestRunDataMigration` (required wraps
tx, none runs on conn, untrusted refused, unknown policy rejected, both
failure paths wrap).

### Offline SQL generation architecture

`generate_offline_sql` (orchestrator.py:2080): calls `topological_sort`,
reads module file content, computes `digest = compute_digest(name, content)`
(matches the ledger), renders forward ops via `_split_ops_by_phase` into
`pre_tx` / `tx` / `post_tx` phases, sets `has_destructive` via
`_is_op_destructive`. No `Connection` argument, no DB I/O. Verified by
`TestGenerateOfflineSql` (digest matches ledger, topological order,
phase annotations, reversible flag, destructive flag, dialect passthrough).

### W1-C preservation (regression)

`_apply_postgres` (orchestrator.py:1400-1511): `pg_advisory_xact_lock`,
`conn.acquire() + raw_conn.transaction()`, `record_applied_on_conn` atomic,
`_is_op_destructive` confirm gate (orchestrator.py:1371-1372), env gate, token
gate — all byte-identical to base revision. `_is_op_destructive`
(orchestrator.py:156-170) and `_split_ops_by_phase` (orchestrator.py:1259)
unchanged. W1-C regression suite passes:
`uv run pytest tests/python/unit/test_migration_gates.py
tests/python/unit/test_migrations_cli.py tests/python/unit/test_migrations.py
tests/python/unit/test_migration_loader.py
tests/python/unit/test_migration_operations.py tests/python/unit/test_sqlmigrate.py
tests/python/unit/test_fts_migrations.py
tests/python/unit/test_makemigrations_autodiff.py -q` → 162 passed.

`MigrationGraph` acquires a `Connection` only for read-only ledger queries
(`status`, `upgrade_plan`, `downgrade_plan`, `recovery_guidance`); it never
calls `apply()`, `_apply_postgres()`, or the CLI. This keeps ADR-004
ownership with W1-C per AGENTS.md §5a.

## Findings

| # | Severity | Evidence | Required correction |
|---|----------|----------|---------------------|
| 1 | info | `MigrationGraph` is read-only and does not apply/revert migrations itself; applying still flows through W1-C CLI. A future task could wire `MigrationGraph` into the CLI to replace ad-hoc graph code, but that touches W1-C/W2-F owned CLI files. | None for W3-A. Documented as follow-up in the executor log. Out of scope by task non-goals. |
| 2 | info | `DataMigration` does not run inside the W1-C advisory-locked apply transaction by default — `run_data_migration` opens its own `conn.transaction()`. A data migration that needs to share the W1-C apply transaction would require CLI integration (W1-C/W2-F owned). | None for W3-A. The task contract requires "explicit transaction policy," which is satisfied. Future CLI integration is out of scope. |

## Decision

`approved`

The topological ordering, target upgrade/downgrade, data-migration callable,
and offline-SQL generation architectures are sound and additive. The W1-C
advisory-lock / transactional-DDL / atomic-ledger / destructive-confirm / env /
token gates are byte-identical to the base revision. ADR-004 ownership stays
with W1-C: the graph layer is read-only and never acquires a connection for
apply/revert. No architectural constraint in AGENTS.md §2 or §5 is violated.
This record grants only the ChiefArchitect gate.
