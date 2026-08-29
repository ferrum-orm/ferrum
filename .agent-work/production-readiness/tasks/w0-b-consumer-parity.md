---
task_id: w0-b-consumer-parity
wave: wave-0
owner: production-readiness-executor
status: verified
run_id: 20260821T075801Z
dependencies: []
owned_paths:
  - tests/consumer_contracts/
security_triage_complete: true
security_surfaces:
  sql_compilation: false
  migration_apply: false
  errors_redaction: false
  auth_secrets: false
  rls_admin_gucs: true
  schema_selection: false
security_review: true
security_review_justification: >-
  Run 20260821T075801Z reproduced ta-16: EnableRLS(force=True) emits FORCE
  without ENABLE. SecurityEngineer approved the inventory/xfail characterization
  (reviews/w0-b-consumer-parity/20260821T075801Z-security-engineer.md). That
  approval does not clear the production defect; ENABLE+FORCE remains W1-C.
architecture_review: true
product_review: false
code_review: true
---

# Task: Consumer parity inventory

## Specify

### Problem

The production plan must distinguish real consumer requirements from SQLAlchemy
behavior that should be removed during async refactoring.

### Scope

Build executable, source-cited Ticket Analyzer and Org AI Platform parity
manifests under `tests/consumer_contracts/`.

### Non-goals

No consumer refactor and no Ferrum production implementation.

### Invariants and failure modes

Inventory both happy and failure paths: concurrency, cancellation, tenancy,
pooling, type fidelity, migration authority, and redaction. Do not infer support
from method names; reproduce or cite it.

### Acceptance criteria

Every audited consumer call path is classified as supported, Ferrum defect,
missing API, or consumer refactor, with source evidence and a target contract.

## Plan

Partition audits by consumer, normalize findings into one schema, review
cross-cutting requirements, and convert highest-risk contracts into executable
tests or explicit skipped contracts with blockers.

## Tasks

1. Inventory Ticket Analyzer persistence call paths.
2. Inventory Org AI Platform persistence call paths.
3. Normalize and deduplicate capability requirements.
4. Add executable contract fixtures/tests.
5. Architecture-review disputed ORM-versus-consumer ownership.

## Implement

Ready for two parallel read-only consumer audits and one Ferrum test integrator.

## Validation contract

Run consumer-contract tests and prove every manifest entry cites a real call path.
Run `mise run ci-local`.

## Independent verification contract

Verifier samples every classification category in both consumers and searches for
uncatalogued persistence patterns.

## Revert contract

Remove only added contract fixtures/tests; no production behavior changes.
