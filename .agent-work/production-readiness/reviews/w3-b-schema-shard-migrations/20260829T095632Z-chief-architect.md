---
task_id: w3-b-schema-shard-migrations
run_id: 20260829T095632Z
authority: ChiefArchitect
reviewer: chief-architect-agent
reviewed_at: 2026-08-29T12:00:00Z
base_revision: 612f476c32fa7b1fbd38e4dc9f4c689d05b72191
decision: approved
scope:
  - python/ferrum/routing.py
  - python/ferrum/migrations/coordinator.py
  - tests/python/unit/test_schema_shard_migrations.py
  - tests/python/integration/test_schema_shard_migrations.py
---

# Named Authority Verdict

## Authority

ChiefArchitect

## Claims reviewed

Coordination architecture for applying one migration graph across selected
schemas/shards (AGENTS.md §2, §5a "Schema tenancy and sharding boundaries";
task contract `w3-b-schema-shard-migrations.md` acceptance criteria §62-73):

- AC1: Apply one migration graph across selected schemas/shards.
- AC2: Per-target advisory locks (reuse W1-C pattern).
- AC3: Bounded concurrency (configurable parallelism).
- AC4: Resumable status (per-target migration state).
- AC5: Fail-fast/continue policy (configurable).
- AC6: Never promise cross-shard atomicity; report partial rollout precisely.
- AC7: Idempotent reruns.
- AC8: Canary-target support.
- AC9: Structured progress hooks.

Architectural invariants (AGENTS.md §2.6, §5a): no implicit multi-DB behavior;
QuerySet stays shard-unaware and connection-explicit; the coordinator receives
explicit `Connection` objects per target — no implicit connection selection
from model metadata, tenant id, or schema name.

## Evidence

### Source inspection

- `git diff HEAD -- python/ferrum/routing.py`: +11 lines, purely additive
  `items()` method returning `list[tuple[str, Connection]]` in registration
  order. No behavior change to existing methods. Verified at
  `python/ferrum/routing.py:281-290`.
- `python/ferrum/migrations/coordinator.py`: 1031 lines, new file. Read in
  full. Structure:
  - `SchemaShardMigrationCoordinator` (`:268-974`)
  - `MigrationTarget` (`:165-188`), `CoordinatorResult` (`:241-256`),
    `TargetResult` (`:223-238`), `TargetMigrationStatus` (`:208-220`),
    `TargetMigrationState` (`:135-142`), `ProgressEvent` (`:191-205`),
    `ProgressEventType` (`:145-159`), `ProgressHook` (`:260`).
- `git diff HEAD -- python/ferrum/migrations/orchestrator.py
  python/ferrum/session.py python/ferrum/migrations/ledger.py
  python/ferrum/migrations/operations.py python/ferrum/__init__.py`:
  **empty** — no modifications to W3-A/W1-C/W1-F-owned files.

### AC1 — Apply one graph across selected schemas/shards

`coordinator.py:396-502` (`run`): iterates `self._targets`, calls
`_run_one_target` for each. `_run_one_target` (`:594-723`) builds a per-target
`MigrationGraph(self._modules, conn=target.connection)` so `upgrade_plan()`
returns only pending migrations for that target's ledger. Schema path
(`:764-781`) wraps apply in `schema_transaction(conn, schema,
allowed_schemas=...)`. ✓

### AC2 — Per-target advisory locks

`coordinator.py:100-127`: `_COORD_LOCK_NAMESPACE =
b"ferrum.migrations.coordinator"`, `_COORD_LOCK_KEY_1` derived from
`sha256(namespace)[0:4]` masked to signed int32 via `_to_int32`.
`_coord_lock_key_2(target_id)` derives key2 from
`sha256(target_id)[0:4]` masked to signed int32. `coordinator.py:881-895`:
inside the apply transaction, `SELECT pg_advisory_xact_lock($1, $2)` with
`_COORD_LOCK_KEY_1, key2` as bound parameters (not interpolated). Lock is
transaction-scoped (`pg_advisory_xact_lock` not `pg_advisory_lock`) so
commit/rollback auto-releases. The namespace key1 is distinct from W1-C's
`ADVISORY_LOCK_KEY_1` (different `sha256` seed) so concurrent W1-C and W3-B
applies on the same DB do not collide. ✓

### AC3 — Bounded concurrency

`coordinator.py:450` `sem = asyncio.Semaphore(self._max_parallelism)`.
Fail-fast path (`:525-584`): queue + worker model with `asyncio.wait(
FIRST_COMPLETED)`, stops scheduling on first failure, cancels pending
workers. Continue path (`:586-592`): `asyncio.gather` with sem.
`max_parallelism` default 4, validated `>= 1` in `__init__` (`:329-332`). ✓

### AC4 — Resumable status

`coordinator.py:373-381`: `self._status: dict[str, dict[str,
TargetMigrationStatus]]` initialized `PENDING` for every target/module pair.
`status()` (`:387-394`) returns snapshot. `_run_one_target` updates state to
`IN_PROGRESS`/`APPLIED`/`SKIPPED`/`FAILED` per migration. Migrations filtered
out by `upgrade_plan()` are marked `SKIPPED` (`:642-646`). ✓

### AC5 — Fail-fast/continue policy

`coordinator.py:525-584` (fail_fast): on first failure, records the result,
cancels remaining workers, raises `FerrumMigrationError` with a partial
rollout summary. `:586-592` (continue): `asyncio.gather` collects all
results, no raise. Policy validated against `_VALID_POLICIES =
{"fail_fast", "continue"}` in `__init__` (`:162, 333`). ✓

### AC6 — Never promise cross-shard atomicity

`coordinator.py:241-256` (`CoordinatorResult` docstring): "Never promises
cross-shard atomicity. `partial_rollout` is True iff some targets applied
migrations and others did not." Each target's apply is independent
(per-target advisory lock, per-target tx, per-target ledger write). No
`BEGIN` spans two targets — `_run_one_target` opens its own
`raw_conn.transaction()` (`:811`) or `schema_transaction` (`:765`) per
target. `partial_rollout` computed at `:481-485`:
`any_progress and any_not_progress`. ✓

### AC7 — Idempotent reruns

Two-level guard: (a) `MigrationGraph(self._modules,
conn=target.connection).upgrade_plan()` (`:615-617`) filters
already-applied migrations from the per-target ledger; (b)
`is_applied_on_conn(raw_conn, digest, dialect="postgres")` (`:898`) inside
the tx catches concurrent races and returns `False` (skip). The ledger
records the file-content digest via `compute_digest(module.name, content)`
(`:757-758`), matching `MigrationGraph.status()` (W3-A). ✓

### AC8 — Canary-target support

`coordinator.py:416-444`: canary phase runs serially before main phase.
On canary failure, sets `halted=True`, emits `CANARY_PHASE_FAILED`, breaks
out of canary loop. Main phase skipped when `halted` (`:449`). Non-run
targets get `TargetResult(halted=True)` (`:459-461`). Canary IDs validated
against target list in `__init__` (`:351-357`). ✓

### AC9 — Structured progress hooks

`coordinator.py:145-160`: 12 `ProgressEventType` values.
`coordinator.py:260`: `ProgressHook = Callable[[ProgressEvent], None] |
Callable[[ProgressEvent], Awaitable[None]]`. `_emit` (`:951-974`): calls
hook, awaits if `Awaitable`, swallows exceptions and logs to stderr. ✓

### Architectural invariant check (§5a)

- **No implicit connection selection**: `MigrationTarget` requires an
  explicit `connection: Connection` (`:186`). The coordinator never resolves
  a connection from model metadata, tenant id, or schema name. ✓
- **No implicit multi-DB behavior**: each target carries its own
  `Connection`; no dialect switching; no `platform_scoped` model flag. ✓
- **QuerySet stays shard-unaware**: the coordinator does not touch
  QuerySet; it operates entirely at the migration-orchestration layer. ✓
- **schema_transaction is validated schema selection on one pinned
  transaction**: `coordinator.py:765-768` calls `schema_transaction(conn,
  schema, allowed_schemas=...)`; `session.py:294-343` validates schema
  against allowlist + identifier regex before opening the tx. ✓
- **No `platform_admin_transaction` misuse**: the coordinator does not
  call `platform_admin_transaction`; it uses `schema_transaction` for
  schema-tenant applies and plain `acquire()` for non-schema applies. ✓

## Findings

| # | Severity | Evidence | Required correction |
|---|----------|----------|---------------------|
| 1 | info | `coordinator.py:62-67, 934` imports private helpers `_op_to_sql`, `_split_ops_by_phase`, `_validate_timeout`, `_is_op_destructive` from `orchestrator.py` (W3-A, complete — import only, no modification). If W3-A changes these, the coordinator breaks. | None blocking. W3-A is complete; the import-only contract is honored. Optional: promote these to a public `orchestrator` API if other consumers appear. |
| 2 | info | `coordinator.py:777` uses `tx._require_driver()` (private method) for the schema-tenant apply path, casting to `Any` to bridge the ledger `_RawConn` protocol vs. driver `execute()` return-type annotation gap. Matches the pattern in `session.py:set_config` but relies on a private API. Documented in the code at `:770-777`. | None blocking. Optional: promote `_require_driver()` to a public protocol method or document the protocol-annotation gap in the ledger's `_RawConn` type. Low risk — W1-F owns `session.py`/`connection.py` and is complete. |
| 3 | info | `coordinator.py:571` cancels remaining fail-fast workers after a failure. Workers currently holding the semaphore finish their target's apply (not cancelled mid-apply) — the code comment at `:528-530` correctly notes cancellation safety is the W1-C contract's responsibility. The cancelled workers are blocked on `queue.get()`, not mid-SQL. | None blocking. Behavior is correct and documented. |
| 4 | info | The coordinator's in-memory `status()` (`:387-394`) is a snapshot of what this coordinator instance observed, not an authoritative view of the per-target ledger. A fresh coordinator re-reads the ledger via `upgrade_plan()`. The docstring at `:388-393` documents this correctly. | None blocking. The resumable-status contract is "track per-target migration state" — in-memory tracking with a documented "fresh authoritative view requires a new coordinator" caveat satisfies AC4. |

No blocking or `changes_required` findings. All findings are `info`-level
and do not affect any acceptance criterion or architectural invariant.

## Decision

`approved`

The coordination architecture satisfies every acceptance criterion (AC1-AC9)
and every architectural invariant in AGENTS.md §5a. Per-target advisory locks
use a distinct namespace from W1-C (no collision). Bounded concurrency is
configurable and correct. Resumable status is in-memory with a documented
caveat. Fail-fast/continue policies are correct. No cross-target transaction
is ever opened — cross-shard atomicity is never promised. Idempotent reruns
have a two-level replay guard. Canary support halts on canary failure.
Progress hooks are sync/async with swallowed errors. The `routing.py` change
is purely additive.

This record grants only the ChiefArchitect gate. It does not substitute for
the SecurityEngineer or CodeReviewer gates, or for independent verification.
