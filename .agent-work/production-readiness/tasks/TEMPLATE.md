---
task_id: replace-me
wave: replace-me
owner: unassigned
status: specified
dependencies: []
owned_paths: []
security_triage_complete: false
security_surfaces:
  sql_compilation: false
  migration_apply: false
  errors_redaction: false
  auth_secrets: false
  rls_admin_gucs: false
  schema_selection: false
security_review: false
security_review_justification: replace-me
architecture_review: false
---

# Task: replace-me

## Specify

### Problem

State the observed defect or missing capability with source evidence.

### Scope

List the behavior and files this task owns.

### Non-goals

List adjacent behavior this task will not change.

### Invariants and failure modes

Record architecture, security, concurrency, performance, compatibility, and
rollback constraints.

### Acceptance criteria

Use observable, falsifiable outcomes.

## Plan

Describe the smallest design, API/IR impact, migration/compatibility impact,
test strategy, and required reviewers. Resolve material choices before Tasks.

## Tasks

List ordered implementation units with file ownership, dependencies, and a
completion criterion for each unit.
Identify shared paths requiring a coordinator lease.

## Implement

Implementation begins only when the coordinator marks this task `ready`.

## Validation contract

List exact focused checks plus the required full `mise run ci-local` gate.

## Independent verification contract

List claims the verifier must reproduce from fresh evidence. The verifier must
not rely only on the executor's summary or exit codes.
List required ChiefArchitect, SecurityEngineer, ProductManager, and CodeReviewer
verdict artifacts under `reviews/<task-id>/<run-id>-<authority>.md`; use
`not_required` only with task-contract justification.

## Revert contract

Describe how to identify and reverse this task without discarding unrelated
work. Never prescribe destructive git commands.
