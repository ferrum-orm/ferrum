# Production Readiness: Next

Load the `ferrum-readiness-orchestration` skill and perform one bounded
coordination iteration:

1. Reconcile completed logs and verification records.
2. Update aggregate state.
3. Complete `Specify → Plan → Tasks` for the highest-priority dependency-ready workstream.
4. Assign only disjoint, ready work.
5. Stop and report evidence, blockers, active ownership, and the next useful action.

Do not implement production code in the coordinator iteration.
Stop before SQL compilation, migration apply, errors/redaction, auth/secrets,
RLS/admin GUCs, schema selection, ADR, or root-contract mutations unless the
canonical named-authority verdict is approved.
