---
name: ferrum-readiness-orchestration
description: Coordinates Ferrum production-readiness waves, task contracts, ownership, state transitions, logs, and independent verification. Use when assigning, resuming, sequencing, or reporting work from the production-readiness plan.
---

# Ferrum Readiness Orchestration

## Load

Read, in order:

1. `AGENTS.md`
2. `.cursor/plans/ferrum-production-readiness_6b5f422d.plan.md`
3. `.agent-work/production-readiness/PROTOCOL.md`
4. `.agent-work/production-readiness/README.md`
5. `.agent-work/production-readiness/state/index.yaml`
6. The selected task contract, workstream state, and latest logs/verifications

## Select work

Choose only a task whose dependencies are complete and whose owned paths do not
overlap active tasks. Grant a coordinator lease before any shared-path edit.
One coordinator edits aggregate state; one executor owns a workstream at a time.

## Canonical task sequence

Every task must complete these artifacts in order:

1. **Specify** — evidence, scope, non-goals, invariants, failure modes, acceptance.
2. **Plan** — design, tradeoffs, API/IR impact, tests, reviewers, rollback.
3. **Tasks** — ordered units, ownership, dependencies, completion criteria.
4. **Implement** — only after the coordinator marks the workstream `ready`.

Use `.agent-work/production-readiness/tasks/TEMPLATE.md`. A heading without
substantive, checkable content is incomplete.

## Dispatch

Give the executor the task id, owned paths, dependency state, task contract, and
required review gates. Require the executor lifecycle:

`Load → Execute → Validate executor output → Verify independently → Update state`

Independent verification is a separate agent/run. Executors may validate their
work but cannot mark it verified.

## Close an iteration

1. Confirm an immutable execution log exists.
2. Confirm independent verification exists or mark `awaiting_verification`.
3. Apply the verifier's state recommendation to aggregate state.
4. Update the plan frontmatter only when a whole plan todo changes status.
5. Report completed evidence, blockers, next ready tasks, and ownership conflicts.

Stop on unresolved architecture/security decisions, failed release gates, or
unexpected edits outside owned paths. Generic verification collects evidence but
never substitutes for required ChiefArchitect, SecurityEngineer, ProductManager,
or CodeReviewer verdicts.
