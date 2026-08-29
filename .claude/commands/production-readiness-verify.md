# Production Readiness: Verify

Select the oldest workstream in `awaiting_verification`, load
`ferrum-readiness-verification`, and independently verify its latest executor run.

Write a new verification record, recommend one state transition, and return
fresh evidence. If no workstream awaits verification, report that fact and stop.
Never self-clear SQL compilation, migration apply, errors/redaction, auth/secrets,
RLS/admin GUCs, schema selection, ADR, or root-contract gates; require approved
canonical named-authority verdicts.
