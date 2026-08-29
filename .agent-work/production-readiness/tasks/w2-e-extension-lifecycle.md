---
task_id: w2-e-extension-lifecycle
wave: wave-2
owner: production-readiness-executor
status: in_progress
run_id: 20260829T093132Z
shared_path_lease: null
dependencies:
  - w1-e-pool-lifecycle
owned_paths:
  - python/ferrum/ext/pgvector.py
  - python/ferrum/ext/__init__.py
  - python/ferrum/drivers/protocol.py
  - tests/python/unit/test_pgvector.py
  - tests/python/integration/test_pgvector_lifecycle.py
security_triage_complete: true
security_surfaces:
  sql_compilation: false
  migration_apply: false
  errors_redaction: false
  auth_secrets: false
  rls_admin_gucs: false
  schema_selection: false
security_review: false
security_review_justification: Extension registration and codec lifecycle; no SQL compilation, credential, or migration surfaces
architecture_review: true
product_review: false
code_review: true
---

# Task: pgvector and extension lifecycle

## Specify

### Problem

pgvector codec registration is manual — `register_vector_codecs(conn)` must be
called after every `ferrum.connect()`. There is no declarative connection
initializer mechanism that runs uniformly for current and future pooled
connections. Extension availability is not checked at pool creation.

### Scope

`python/ferrum/ext/pgvector.py`, `python/ferrum/ext/__init__.py`,
`python/ferrum/drivers/protocol.py` (initializer protocol), and owned tests.
Generalize the initializer mechanism for citext or consumer-defined codecs
without exposing raw connections.

### Non-goals

No `connection.py` / `drivers/postgres.py` edits (W1-E owns, complete — the
initializer protocol lives in `protocol.py` and is consumed by the driver; do
NOT modify `postgres.py`). No `__init__.py` edits (shared path, no lease). No
QuerySet changes. No `README.md` / `CHANGELOG.md` (record bullets in log).

### Invariants and failure modes

Connection initializers run uniformly for current and future pooled connections.
Optional `connect(..., extensions=[pgvector])` enables initialization. Explicit
dimensions/metric typing preserved. Insert/update/bulk/KNN paths verified. Pool
growth, reconnect/failover, initializer failure, and mixed extension availability
tested. Initializers must not expose raw connections to consumer code.

### Acceptance criteria

- Declarative connection initializers that run on every new pooled connection.
- Optional `connect(..., extensions=[pgvector])` or similar mechanism.
- Pool growth, reconnect/failover runs initializers on new connections.
- Initializer failure handling (fail-closed or raise).
- Insert/update/bulk/KNN paths verified with pgvector.
- Generalized mechanism for citext or consumer-defined codecs.
- Pool growth, reconnect/failover, initializer failure, mixed availability tests.

## Plan

Add a `ConnectionInitializer` protocol in `protocol.py`. Extend `ext/pgvector.py`
with a declarative initializer. Add `connect(..., extensions=...)` documentation
(the actual connect function is in `connection.py` — do NOT modify it; the
initializer protocol is consumed at the driver level via `protocol.py`).
ChiefArchitect for the initializer architecture; CodeReviewer required.
SecurityEngineer not required (no credential/SQL/migration surfaces).

## Tasks

1. Audit existing `ext/pgvector.py` and identify gaps vs the plan.
2. Add `ConnectionInitializer` protocol in `protocol.py`.
3. Add declarative pgvector initializer in `ext/pgvector.py`.
4. Verify insert/update/bulk/KNN paths with pgvector.
5. Generalize for citext or consumer-defined codecs.
6. Test pool growth, reconnect/failover, initializer failure, mixed availability.
7. Focused checks plus `mise run ci-local`.

## Implement

Coordinator marked `in_progress` at `20260829T093132Z` with exclusive owned paths
and no shared-path lease. Implement the Tasks section.

## Validation contract

Focused pgvector unit tests plus live pgvector integration tests against
PostgreSQL, then `mise run ci-local`.

## Independent verification contract

Verifier proves initializers run on new connections (pool growth/reconnect),
initializer failure handling, and pgvector KNN paths. Named gates: ChiefArchitect,
CodeReviewer. SecurityEngineer `not_required`. ProductManager `not_required`.

## Revert contract

Revert only owned ext/protocol/test files from this run. Preserve all other
workstreams.
