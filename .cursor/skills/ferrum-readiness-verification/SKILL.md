---
name: ferrum-readiness-verification
description: Independently verifies one Ferrum production-readiness executor run using fresh source, test, concurrency, security, and rollback evidence. Use when a workstream is awaiting verification.
---

# Ferrum Readiness Verification

Load `.agent-work/production-readiness/PROTOCOL.md`; it is authoritative.

## Load

Read the task contract, workstream state, executor log, changed files, plan gates,
and applicable rules. Treat the executor summary as claims, not evidence.

## Verify independently

1. Restate every acceptance claim as a falsifiable proposition.
2. Inspect the actual diff and affected call paths.
3. Run fresh focused checks; use live PostgreSQL for driver, SQL, migration,
   tenancy, locking, cancellation, or type-fidelity claims.
4. Exercise relevant failure modes and verify errors/logs contain no secrets,
   DSNs, bound values, or row data.
5. Check that revert instructions are sufficient and preserve unrelated work.
6. Confirm no files outside task ownership changed without coordinator approval.
7. Run every deterministic gate named by the task, including full
   `mise run ci-local`; record meaningful output, not only exit status.
8. Confirm required ChiefArchitect, SecurityEngineer, ProductManager, and
   CodeReviewer verdict artifacts exist at the canonical `reviews/` paths and
   record `decision: approved`. Do not grant those clearances.

SecurityEngineer approval is mandatory for SQL compilation, migration apply,
errors/redaction, auth/secrets, RLS/admin GUCs, and schema selection.

Use `.agent-work/production-readiness/verification/TEMPLATE.md`. Write a new
record; never modify executor logs.

## Decision

- `verified`: every acceptance criterion has fresh deterministic evidence and
  every required named-authority verdict passes.
- `changes_required`: a reproducible defect, missing evidence, scope leak, or doc/test gap exists.
- `blocked`: required environment, dependency, architecture, or security decision is unavailable.

The verifier recommends a state transition. Only the coordinator edits aggregate state.
