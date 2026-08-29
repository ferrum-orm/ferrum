---
name: ferrum-readiness-execution
description: Executes one assigned Ferrum production-readiness task with owned paths, durable logs, validation evidence, safe rollback notes, and state updates. Use after a workstream task is marked ready.
---

# Ferrum Readiness Execution

Execute one ready task; do not absorb adjacent work.
Load `.agent-work/production-readiness/PROTOCOL.md`; it is authoritative.

## 1. Load phase

- Read `AGENTS.md`, the production-readiness plan, task contract, workstream
  state, relevant rules, and prior logs.
- Verify dependencies, `ready` status, and exclusive path ownership.
- Verify a coordinator lease exists before editing any shared path.
- Capture base revision and pre-existing changes. Preserve user and other-agent work.
- Create a new run log from
  `.agent-work/production-readiness/logs/TEMPLATE.md`.

Completion: every required input and pre-existing change is accounted for.

## 2. Execute

- Implement only the approved Tasks section.
- Keep tests and public documentation in the same change.
- Record changed paths, decisions, deviations, and blockers in the run log.
- Stop before crossing an unowned path or unresolved architecture/security gate.

Completion: implementation acceptance criteria are represented by code/tests/docs.

## 3. Validate executor output

- Run focused format, lint, type, unit, integration, security, and live-Postgres
  checks proportional to the task.
- Run `mise run ci-local` before claiming task completion.
- Record exact commands, exit status, and meaningful output. Investigate failures;
  do not relabel them as unrelated without evidence.
- Update workstream state to `awaiting_verification` only when executor validation passes.

Completion: each acceptance criterion has recorded evidence or an explicit blocker.

## 4. Independent verification

Request `production-readiness-verifier` with the task contract and run id. Do not
edit its verification record or mark your own work verified.
Required ChiefArchitect, SecurityEngineer, ProductManager, and CodeReviewer gates
remain separate and must be recorded before completion.

Completion: verifier decision is `verified`, `changes_required`, or `blocked`.

## 5. State and handoff

- Finish the immutable run log, including safe inverse changes.
- Update only the owned workstream state. The coordinator updates aggregate state.
- On changes required, create a new run; never rewrite prior logs.
- Report files, evidence, residual risks, verifier decision, and next action.
