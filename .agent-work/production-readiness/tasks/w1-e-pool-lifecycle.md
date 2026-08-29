---
task_id: w1-e-pool-lifecycle
wave: wave-1
owner: unassigned
status: tasked
run_id: null
shared_path_lease: null
dependencies:
  - w1-d-error-taxonomy
owned_paths:
  - python/ferrum/drivers/postgres.py
  - python/ferrum/connection.py
  - tests/python/unit/test_asyncpg_json_codecs.py
  - tests/python/integration/test_connection.py
  - tests/python/integration/test_connection_runtime.py
security_triage_complete: true
security_surfaces:
  sql_compilation: false
  migration_apply: false
  errors_redaction: false
  auth_secrets: true
  rls_admin_gucs: false
  schema_selection: false
security_review: true
security_review_justification: TLS/DSN diagnostics and pool error paths must not leak credentials
architecture_review: true
product_review: false
code_review: true
---

# Task: Pool lifecycle and configuration

## Specify

### Problem

`acquire_timeout` is not enforced on every path. Idle lifetime vs hard max age
are poorly named. Shutdown busy-polls. There is no typed `PoolStats` snapshot,
and failover/stale-connection replacement is untested.

### Scope

`drivers/postgres.py` and `connection.py` pool/lifecycle/config, plus owned
connection tests. Coordinator will not grant `connection.py` to W1-B until this
workstream verifies.

### Non-goals

No `run_transaction` / statement-retry policy (W1-B). No QuerySet changes.
No `__init__.py` / README edits unleased. Do not pre-ping every query unless
benchmarks later justify it.

### Invariants and failure modes

Acquire waits honor `acquire_timeout` including convenience `fetch`/`execute`.
DSN passwords never appear in diagnostics (host/port/database/username/
category only). Shutdown must close streams deterministically and report forced
drain timeout without leaking connections. Concurrent acquire/cancel/failover
must not double-release or hang waiters.

### Acceptance criteria

- `acquire_timeout` on every acquire path.
- Distinct idle lifetime vs hard max age; configurable command timeout,
  statement cache, TLS/SSL, `server_settings`, `application_name`.
- Failover-safe validation/replacement; no unconditional pre-ping.
- Event/condition shutdown instead of busy polling; drain timeout; close
  active streams.
- Typed `PoolStats`: size, idle, acquired, waiters if available, min/max,
  in-flight, accepting/closing.
- Tests: saturation, cancellation, failover/restart, stale connection, growth,
  shutdown.

## Plan

Implement pool config and lifecycle in the asyncpg driver and Connection
wrapper after W1-D so pool-exhaustion errors use structured `category`.
ChiefArchitect for the stats/shutdown contract; SecurityEngineer for TLS/DSN
redaction; CodeReviewer required.

## Tasks

1. Wait for W1-D verified.
2. Enforce acquire_timeout; split idle vs max age; add config knobs.
3. Replace busy-poll shutdown; close streams; PoolStats.
4. Saturation/cancel/failover/stale/growth/shutdown live tests.
5. `mise run ci-local`. Docs via later README lease.

## Implement

Implementation begins only when the coordinator marks this task `ready`.

## Validation contract

Focused driver/connection tests plus live pool tests, then `mise run ci-local`.

## Independent verification contract

Verifier proves acquire_timeout, shutdown drain, and DSN redaction from fresh
live PostgreSQL evidence. Named gates: ChiefArchitect, SecurityEngineer,
CodeReviewer. ProductManager `not_required`.

## Revert contract

Revert only owned driver/connection/test files from this workstream's runs.
Preserve W1-D error taxonomy.
