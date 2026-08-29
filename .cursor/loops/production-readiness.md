# Ferrum Production Readiness Loop

Use dynamic recurrence:

`/loop /production-readiness-loop`

Each tick advances at most one coordination boundary and re-arms only when useful.
Primary wakes are executor completion, verifier completion, or CI completion.
Stop on user/architecture/security/environment blockers, failed ownership checks,
or completion of currently authorized work.
SQL compilation, migration apply, errors/redaction, auth/secrets, RLS/admin GUCs,
schema selection, ADRs, and root contracts require named-authority verdicts before
the loop can continue. The loop never ratifies, commits, pushes, merges, or self-clears.

The loop coordinates; individual executors still follow the canonical task and
run lifecycles and maintain durable state/logs.
