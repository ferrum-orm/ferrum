---
task_id: w3-b-schema-shard-migrations
run_id: 20260829T095632Z
authority: SecurityEngineer
reviewer: security-engineer-agent
reviewed_at: 2026-08-29T12:15:00Z
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

SecurityEngineer

## Claims reviewed

Security-gated surfaces touched by W3-B (AGENTS.md §3, §5a; task contract
`security_surfaces`: `migration_apply: true`, `schema_selection: true`):

- S1: Per-target advisory lock safety — keys derived from a fixed Ferrum
  namespace + per-target hash; never from user input; transaction-scoped
  (`pg_advisory_xact_lock`); distinct namespace from W1-C.
- S2: `schema_transaction` integration — schema selection uses allowlist +
  identifier regex; `search_path` set via bound parameter, transaction-local;
  never interpolated from untrusted input.
- S3: Destructive gate preserved — MIG-2 (destructive) and MIG-5 (non-dev
  env) gates scanned independently via `_is_op_destructive` before any SQL
  is emitted.
- S4: No secret/DSN/row data leakage in progress hooks or error reporting —
  Tier A only (AGENTS.md §3).
- S5: `_sanitized_category` returns mapped exception `category`; never raw
  DETAIL/HINT or bound values (AGENTS.md §5a "Safe error fields").

## Evidence

### Source inspection

- `python/ferrum/migrations/coordinator.py`: 1031 lines, new file. Read in
  full with focus on advisory-lock key derivation (`:86-127`), schema
  path (`:764-781`), destructive gate (`:921-945`), error sanitization
  (`:982-1018`), and progress events (`:191-205`).
- `python/ferrum/session.py:294-343` (`schema_transaction`): validates
  schema against `ALLOWED_SCHEMA_NAMES` (default `{"public"}`) AND
  identifier regex `^[a-zA-Z_][a-zA-Z0-9_]{0,62}$` via
  `_validate_schema_name` (`:219`) before opening the tx. `search_path`
  set via `set_config('search_path', $1, true)` — bound parameter,
  transaction-local. Confirmed no string interpolation of the schema name
  into SQL.
- `python/ferrum/migrations/orchestrator.py:156` (`_is_op_destructive`):
  imported (not modified) by the coordinator. The coordinator's
  `_check_gates` (`:921-945`) scans `ops_dicts` independently.

### S1 — Per-target advisory lock safety

`coordinator.py:100-127`: `_COORD_LOCK_NAMESPACE =
b"ferrum.migrations.coordinator"` (fixed, hardcoded — never user input).
`_COORD_LOCK_KEY_1` derived from `sha256(namespace)[0:4]` masked to signed
int32 via `_to_int32`. `_coord_lock_key_2(target_id)` derives key2 from
`sha256(target_id.encode())[0:4]` masked to signed int32. `target_id` is a
trusted caller-supplied identifier (shard name or caller-chosen label) —
never user input and never a secret.

`coordinator.py:881-895`: inside the apply transaction,
`SELECT pg_advisory_xact_lock($1, $2)` with `_COORD_LOCK_KEY_1, key2` as
**bound parameters** (not interpolated into SQL text). Lock is
transaction-scoped (`pg_advisory_xact_lock` not `pg_advisory_lock`) so
commit/rollback auto-releases — no lock leakage across pooled connections.

The namespace key1 (`sha256(b"ferrum.migrations.coordinator")`) is distinct
from W1-C's `ADVISORY_LOCK_KEY_1` (different `sha256` seed in `ledger.py`),
so concurrent W1-C and W3-B applies on the same PostgreSQL cluster do not
collide or block each other. ✓

`_to_int32` (`:104-113`) correctly masks unsigned 32-bit hash bytes to
signed int32 (`value - 2**32` when `value >= 2**31`) so asyncpg accepts the
int4 bind parameter. Unit test `test_key_is_signed_int32`
(`test_schema_shard_migrations.py:176`) asserts `-(2**31) <= key < 2**31`.
✓

### S2 — schema_transaction integration

`coordinator.py:764-781`: when `target.schema is not None`, the apply is
wrapped in `schema_transaction(target.connection, target.schema,
allowed_schemas=target.allowed_schemas)`. `session.py:294-343` validates
the schema name against `_validate_schema_name` (`:219`) which checks:
1. `ALLOWED_SCHEMA_NAMES` (default `{"public"}`) OR the caller-supplied
   `allowed_schemas` frozenset.
2. Identifier regex `^[a-zA-Z_][a-zA-Z0-9_]{0,62}$`.

A schema name failing either check raises `FerrumCompileError` **before**
the transaction opens — fail-closed, no SQL emitted. The `search_path` is
set via `set_config('search_path', $1, true)` — bound parameter,
transaction-local (third arg `true`), so the GUC resets on commit/rollback
and never leaks onto a pooled connection.

The coordinator never accepts schema names from untrusted input — the
`MigrationTarget.schema` field is documented at `:178-183` as requiring a
trusted allowlist. The coordinator delegates validation entirely to
`schema_transaction`, so an unallowed schema name fails closed before any
SQL is emitted. ✓

Schema-tenant constraint (`coordinator.py:861-868`): when `schema_tx_open
= True`, pre-tx/post-tx non-transactional ops (CREATE EXTENSION, CREATE
INDEX CONCURRENTLY) are rejected because they cannot run inside a
`schema_transaction`. This prevents a partial-schema-apply where the
`search_path` GUC would not cover the non-transactional op. ✓

### S3 — Destructive gate preserved

`coordinator.py:921-945` (`_check_gates`): scans `ops_dicts` independently
via `_is_op_destructive` (imported from `orchestrator.py:156`). Raises
`FerrumMigrationError` if any destructive op and `not self._confirm`. Non-dev
env (`self._env != "development"`) requires `confirm=True`. Called at
`:762` **before any SQL is emitted** (before `schema_transaction` or
`acquire()`).

This reuses the W1-C closure of the §3 destructive gate: `alter_column`
SET NOT NULL / type narrowing hits the confirm gate because
`_is_op_destructive` is the orchestrator's kind-set-based classifier (not
a plan flag). The coordinator does not trust a `requires_confirmation`
flag — it scans the ops itself. ✓

Unit tests `test_destructive_without_confirm_raises` (`:517`) and
`test_non_dev_without_confirm_raises` (`:546`) pass. ✓

### S4 — No secret/DSN/row data leakage (Tier A only)

`ProgressEvent` (`:191-205`): `event_type`, `target_id`, `migration_name`,
`applied_count`, `pending_count`, `error: str`. No DSN, password, bound
value, or row data fields. `error` carries a sanitized category via
`_sanitized_category` (`:982-994`).

`TargetMigrationStatus` (`:208-220`): `target_id`, `migration_name`,
`state`, `error: str`. Same sanitized-category contract.

`TargetResult` (`:223-238`): `target_id`, `applied`, `skipped`, `failed`,
`halted`. No secret fields.

`CoordinatorResult` (`:241-256`): `targets`, `canary_results`, `policy`,
`halted`, `partial_rollout`. No secret fields.

The fail-fast exception message (`:578-583`) includes `target_id`,
`migration_name`, and `category` — all sanctioned safe-error-fields
(AGENTS.md §5a). No DSN, password, bound value, or row data. ✓

### S5 — Sanitized error category

`_sanitized_category` (`:982-994`): uses
`getattr(map_db_error(exc), "category", None)` and falls back to
`type(exc).__name__`. Never includes raw PostgreSQL `DETAIL`/`HINT`, bound
parameter values, or row data. The mapped exception's `category` attribute
is the sanctioned safe-error-field (AGENTS.md §5a "Safe error fields":
`sqlstate`, `category`, `constraint`, `model`/`operation`).

`_apply_error` (`:997-1018`): delegates to
`ferrum.errors.migration_op_failure` which produces sanitized messages
(no DETAIL/HINT, no bound values). The migration name is annotated with
`target_id` (`f"{module.name}@{target.target_id}"`) for multi-target log
disambiguation — `target_id` is a trusted caller-supplied identifier, not
a secret. ✓

### SET LOCAL timeout interpolation check

`coordinator.py:871-874`: `SET LOCAL lock_timeout`/`statement_timeout` use
f-string interpolation of `self._lock_timeout`/`self._statement_timeout`.
These are validated by `_validate_timeout` (`:359-360` in `__init__`,
imported from `orchestrator.py:187`) **at construction time**, before any
run. The interpolation is over a validated string — not user input. This
matches the W1-C pattern in `orchestrator.py`. The `_validate_timeout`
gate runs at construction, so a malicious timeout string is rejected before
any coordinator run begins. ✓

### Test execution

```
uv run pytest tests/python/unit/test_schema_shard_migrations.py -x -q
```
Output:
```
...........................                                              [100%]
27 passed in 0.23s
```
Exit: 0. ✓

## Findings

| # | Severity | Evidence | Required correction |
|---|----------|----------|---------------------|
| 1 | info | `coordinator.py:871-874` sets `SET LOCAL lock_timeout`/`statement_timeout` via f-string interpolation of `self._lock_timeout`/`self._statement_timeout`. These are validated by `_validate_timeout` at construction time (`:359-360`), so the interpolation is over a validated string — not user input. Matches the W1-C pattern. | None blocking. The `_validate_timeout` gate runs at construction time, before any SQL emission. Documented at `:870`. Optional: use bound parameters (`SET LOCAL lock_timeout = $1`) for defense-in-depth, but this is a style preference, not a security gap. |
| 2 | info | `coordinator.py:777` uses `tx._require_driver()` (private method) and casts to `Any` for the schema-tenant apply path. This is a code-quality concern (noted by ChiefArchitect), not a security concern — the driver is the same raw connection that holds the tx and the GUC, so there is no privilege boundary crossed. | None blocking. Security-neutral. |

No blocking or `changes_required` findings. All findings are `info`-level.
No secret/DSN/row data leakage. No SQL injection vector. Schema selection
is allowlist + regex validated. Advisory lock keys are derived from a fixed
namespace. Destructive and non-dev gates are preserved and scanned
independently.

## Decision

`approved`

The W3-B coordinator satisfies every security claim (S1-S5). Per-target
advisory locks use a fixed Ferrum namespace and bound parameters.
`schema_transaction` integration validates schema selection against an
allowlist + identifier regex before any SQL is emitted. The destructive
gate (MIG-2) and non-dev env gate (MIG-5) are scanned independently via
`_is_op_destructive` before any SQL is emitted. No secrets, DSNs, bound
values, or row data appear in progress events, results, or error messages
(Tier A only). Error categories are sanitized via `map_db_error`'s
`category` attribute — the sanctioned safe-error-field.

This record grants only the SecurityEngineer gate. It does not substitute
for the ChiefArchitect or CodeReviewer gates, or for independent
verification.
