# Production Readiness: Loop Tick

Load `ferrum-readiness-loop` and execute exactly one bounded tick. Reconcile state,
advance ready work, request missing verification, and choose the next useful wake
condition. Stop on a blocker or ownership overlap.

To run repeatedly, invoke:

`/loop /production-readiness-loop`

Dynamic cadence is preferred because subagent completion, CI, and explicit
blockers determine when another iteration is useful.

Each tick stops before SQL compilation, migration apply, errors/redaction,
auth/secrets, RLS/admin GUCs, schema selection, ADR, or root-contract mutations
unless the canonical named-authority verdict is approved.
