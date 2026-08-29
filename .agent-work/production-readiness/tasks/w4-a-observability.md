---
task_id: w4-a-observability
wave: wave-4
owner: production-readiness-executor
status: in_progress
run_id: 20260829T091235Z
shared_path_lease: null
dependencies:
  - w1-d-error-taxonomy
  - w1-e-pool-lifecycle
owned_paths:
  - python/ferrum/observability.py
  - python/ferrum/hooks.py
  - tests/python/unit/test_observability.py
  - tests/python/unit/test_hooks_observability.py
  - tests/python/integration/test_observability_integration.py
security_triage_complete: true
security_surfaces:
  sql_compilation: false
  migration_apply: false
  errors_redaction: true
  auth_secrets: false
  rls_admin_gucs: false
  schema_selection: false
security_review: true
security_review_justification: Telemetry payloads must not leak bound values, DSNs, credentials, or row data under default or opt-in tiers
architecture_review: true
product_review: false
code_review: true
---

# Task: Real OpenTelemetry and metrics

## Specify

### Problem

Current observability creates zero-duration event spans instead of one span around
actual query execution. There are no pool/transaction/migration/retry/timeout/error
metrics from Tier-A-safe fields. Query fingerprints may leak into default metric
labels. No Prometheus/OTel examples.

### Scope

`python/ferrum/observability.py`, `python/ferrum/hooks.py` (W1-D complete — ownership
transferred), and owned observability tests.

### Non-goals

No `errors.py` edits (W1-D owns, complete — import `sqlstate`/`category` only). No
`connection.py` / `drivers/postgres.py` edits (W1-E owns, complete — use hooks/events
only). No `__init__.py` edits (shared path, no lease). No `README.md` / `CHANGELOG.md`
(record bullets in log). No performance benchmarks (W4-B).

### Invariants and failure modes

One span around actual query execution (not zero-duration events). Low-cardinality
metrics from Tier-A-safe fields. Query fingerprints NOT in default metric labels
(opt-in exemplars/sampling only). No values, DSNs, credentials, or row data under
default telemetry. Pool acquire/release/wait/timeout/shutdown events from real
lifecycle points. Preserve W1-D Tier-A hook contract and redaction layer.

### Acceptance criteria

- One span around actual query execution with ambient parent context.
- Low-cardinality query, transaction, pool, migration, retry, timeout, and error
  metrics from Tier-A-safe fields.
- Query fingerprints NOT in default metric labels; opt-in exemplars/sampling to avoid
  cardinality explosions.
- Pool acquire/release/wait/timeout and shutdown events from real lifecycle points.
- Prometheus/OTel examples and verification.
- No values, DSNs, credentials, or row data under default telemetry (security tests).
- W1-D Tier-A hook contract and redaction layer preserved.

## Plan

Rewrite `observability.py` to create one span around query execution. Emit metrics
from Tier-A-safe fields via hooks. Add pool lifecycle events from W1-E's event-based
shutdown. ChiefArchitect for the observability architecture; SecurityEngineer for
telemetry payload redaction; CodeReviewer required.

## Tasks

1. Audit existing `observability.py` and `hooks.py` and identify gaps vs the plan.
2. Create one span around actual query execution with ambient parent context.
3. Emit low-cardinality metrics from Tier-A-safe fields (query/transaction/pool/
   migration/retry/timeout/error).
4. Keep query fingerprints out of default metric labels; add opt-in exemplars/sampling.
5. Emit pool acquire/release/wait/timeout/shutdown events from real lifecycle points.
6. Add Prometheus/OTel examples.
7. Security tests proving no values/DSNs/credentials/row data under default telemetry.
8. Preserve W1-D Tier-A hook contract and redaction layer.
9. Focused checks plus `mise run ci-local`.

## Implement

Coordinator marked `in_progress` at `20260829T091235Z` with exclusive owned paths and
no shared-path lease. Implement the Tasks section.

## Validation contract

Focused observability/hooks unit tests plus live observability integration tests
against PostgreSQL, then `mise run ci-local`. Record span durations and metric
payloads.

## Independent verification contract

Verifier proves span-around-query, metric cardinality, telemetry payload redaction
(no secrets/DSNs/values/row data), and W1-D Tier-A hook contract preservation. Named
gates: ChiefArchitect, SecurityEngineer, CodeReviewer `decision: approved`.
ProductManager `not_required`.

## Revert contract

Revert only owned observability/hooks/test files from this run. Preserve all other
workstreams.
