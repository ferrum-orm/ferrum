---
name: ferrum-readiness-loop
description: Runs one bounded Ferrum production-readiness coordination iteration and chooses the next useful wake condition. Use with /loop when continuing implementation, validation, or verification across waves.
disable-model-invocation: true
---

# Ferrum Readiness Loop

Load `.agent-work/production-readiness/PROTOCOL.md`; it is authoritative.
Each tick performs one bounded orchestration iteration.

1. Load `.agent-work/production-readiness/state/index.yaml`.
2. Reconcile completed executor/verifier results into aggregate state.
3. Surface blockers before assigning new work.
4. Dispatch only dependency-ready, non-overlapping workstreams.
5. Run or request missing independent verification.
6. Update state and report evidence plus the next wake condition.

Do not create work merely to fill a tick. Stop the loop when:

- all currently authorized work is complete;
- user input, architecture, security, credentials, or an external environment is required;
- a release gate fails and needs a scoped corrective task;
- continuing would overlap an active workstream.
- the next action changes SQL compilation, migration apply, errors/redaction,
  auth/secrets, RLS/admin GUCs, schema selection, an ADR, or a root contract and
  the required named-authority verdict is absent.

Prefer event-driven wakes for subagent completion or CI. Use a long fallback
heartbeat only when an active operation can make progress without user input.
Never run multiple implementation ticks concurrently against the same workstream.
The loop may draft gate material but never ratifies contracts, commits, pushes,
merges, or grants architecture/security approval.
