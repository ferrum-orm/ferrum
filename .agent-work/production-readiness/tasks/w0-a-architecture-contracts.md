---
task_id: w0-a-architecture-contracts
wave: wave-0
owner: production-readiness-executor
status: verified
run_id: 20260821T085200Z
shared_path_lease: null
dependencies: []
owned_paths:
  - AGENTS.md
  - CLAUDE.md
  - README.md
  - CHANGELOG.md
  - .cursor/plans/ferrum-production-readiness_6b5f422d.plan.md
  - .cursor/rules/project.md
  - .claude/rules/project.md
security_triage_complete: true
security_surfaces:
  sql_compilation: false
  migration_apply: true
  errors_redaction: true
  auth_secrets: false
  rls_admin_gucs: true
  schema_selection: true
security_review: true
security_review_justification: contract changes cover migration, redaction, tenancy GUC, and schema routing
architecture_review: true
product_review: true
code_review: true
---

# Task: Architecture and compatibility contracts

## Specify

### Problem

Ferrum's documented ADR-004 contract differs from migration runtime behavior, and
retry, diagnostics, schema tenancy, sharding, and compatibility boundaries need
approved contracts before implementation.

### Pre-existing draft

The orchestration bootstrap already changed the owned contract files and reopened
ADR-004 as an unratified draft. Load and attribute both bootstrap logs before
editing:

- `.agent-work/production-readiness/logs/orchestration-bootstrap/20260821T073100Z.md`
- `.agent-work/production-readiness/logs/orchestration-bootstrap/20260821T074435Z.md`
- `.agent-work/production-readiness/logs/orchestration-bootstrap/20260821T074737Z.md`
- `.agent-work/production-readiness/logs/orchestration-bootstrap/20260821T075533Z.md`

This task owns review, correction, named-authority ratification, and any remaining
contract gaps; it must not represent bootstrap wording as an approved decision.

### Scope

Specify the Wave 0 architecture decisions and update only the owned contract files.

### Non-goals

No production code, migrations, consumer edits, or release declaration.

### Invariants and failure modes

Keep Ferrum async-native, PostgreSQL-first, connection-explicit, Rust pure/sync,
and free of raw-SQL escape hatches. Surface migration partial failure, transaction
replay, cross-tenant routing, redaction, and compatibility break risks.

### Acceptance criteria

- ADR-004 text matches observed runtime and records its closure criteria.
- Retry, error-field, tenancy/routing, and stability boundaries are explicit.
- SecurityEngineer and ChiefArchitect provide recorded decisions.
- ProductManager records the compatibility-policy decision.

## Plan

Compare source against contract claims, draft the smallest authoritative updates,
obtain architecture/security verdicts, then independently verify every claim
against source.

## Tasks

1. Cite current behavior for each disputed contract.
2. Draft decision text and non-goals.
3. Obtain architecture and security review.
4. Apply approved contract updates.
5. Validate links, contradictions, and terminology.

## Implement

Ready for assignment; contract/document changes only.

## Validation contract

Search for contradictory ADR/retry/tenancy/stability claims and run documentation
checks plus `mise run ci-local`.

## Independent verification contract

Verifier checks every behavioral statement against current source and confirms
architecture, security, product, and code-review records exist.

## Revert contract

Reverse only this task's contract paragraphs; preserve unrelated learned facts and
agent-orchestration documentation.
