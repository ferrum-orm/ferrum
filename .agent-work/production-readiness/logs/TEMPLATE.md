---
task_id: replace-me
run_id: replace-me
executor: replace-me
started_at: replace-me
finished_at: null
base_revision: replace-me
commit_revision: null
result: in_progress
---

# Execution log

## Load

- Task contract:
- Workstream state:
- Dependencies:
- Owned paths:
- Relevant rules, plans, and prior logs:
- Pre-existing working-tree changes that must be preserved:

## Execute

Record decisions, changed paths, and material deviations from the task plan.

## Validate executor output

For every command, record the exact command, exit status, and meaningful output.
Distinguish focused checks from the full `mise run ci-local` gate. Never infer
success from an exit status alone when output proves the claim.

## Result

- Acceptance criteria satisfied:
- Remaining failures or blockers:
- Risks and follow-up:

## Revert

List files and behavior introduced by this run and the smallest safe inverse
change. Preserve unrelated working-tree changes. After commit, record its SHA so
Git can provide durable history; do not commit unless the user requested it.

## State transition

Record old status, new status, timestamp, and the state file updated.
