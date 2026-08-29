# Production Readiness Execution State

This directory is the durable control plane for
`.cursor/plans/ferrum-production-readiness_6b5f422d.plan.md`.

## Sources of truth

- `PROTOCOL.md` is the authoritative lifecycle, authority, ownership, evidence,
  state-transition, and loop contract shared by Cursor and Claude.
- The plan defines scope, dependencies, and release gates.
- `state/index.yaml` is coordinator-owned aggregate state.
- `tasks/<task-id>.md` is the contract for one assigned task. Its frontmatter
  status is coordinator-owned and mirrors aggregate dispatch status.
- `state/workstreams/<task-id>.yaml` is executor-owned task state.
- `logs/<task-id>/<run-id>.md` is an append-only-by-protocol execution record.
- Coordinator/bootstrap runs use their operation id as `<task-id>`.
- `verification/<task-id>/<run-id>.md` is an independent verifier decision.
- `reviews/<task-id>/<run-id>-<authority>.md` is one named-authority verdict
  following `reviews/TEMPLATE.md`.
- `state/LEASE_TEMPLATE.yaml` defines coordinator-issued shared-path leases.

## Write ownership

- The coordinator alone edits `state/index.yaml`.
- The coordinator edits task-contract frontmatter status and immutable contract
  sections before assignment; an assigned executor may append implementation
  evidence only within its task file.
- One executor owns one workstream state file and log directory at a time.
- A verifier writes only under `verification/`.
- A named authority owns its verdict content under `reviews/`. If read-only, the
  coordinator persists its returned verdict verbatim with the authority agent id.
- Existing logs and verification records are append-only by protocol. Corrections
  use a new run; Git provides durable history after commit.

`STATE.md` at the repository root is not orchestration state. The only aggregate
state is `state/index.yaml`.

## Task contract lifecycle

Every task records `Specify → Plan → Tasks → Implement`. The depth is
proportional to risk, but no stage is skipped.

Implementation may begin only after the task contract records completed Specify,
Plan, and Tasks sections and the aggregate state marks it `ready`.

## Executor run lifecycle

Every run records `Load → Execute → Validate executor output → Verify
independently → Update state`, as defined in `PROTOCOL.md`.

## Identifiers and review artifacts

- `run-id` is a UTC basic timestamp: `YYYYMMDDTHHMMSSZ`.
- Required named reviews are recorded under the workstream's `artifacts` map.
- Only an `approved` verdict at the canonical review path clears its named gate.
- `implementation_complete` does not imply completion; deterministic verification
  and every required named review must pass first.
