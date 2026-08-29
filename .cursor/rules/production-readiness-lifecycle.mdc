---
description: Enforce the canonical lifecycle for Ferrum production-readiness tasks
alwaysApply: true
---

# Production-readiness lifecycle

For work derived from
`.cursor/plans/ferrum-production-readiness_6b5f422d.plan.md`:

0. Load `.agent-work/production-readiness/PROTOCOL.md`; it is authoritative.
1. Complete `Specify → Plan → Tasks → Implement` in the task contract.
2. Begin implementation only when dependencies pass, paths are exclusively owned,
   and coordinator state is `ready`.
3. Every executor run follows
   `Load → Execute → Validate executor output → Verify independently → Update state`.
4. Executor validation and independent verification are distinct gates. An executor
   never marks its own task verified.
5. Tests and docs ship with behavior. Run focused checks during development and
   full `mise run ci-local` before completion.
6. Security-sensitive work requires SecurityEngineer review; architecture and new
   failure-mode work requires ChiefArchitect review.
7. Generic verifiers collect deterministic evidence; they never replace named
   authority verdicts or ratify ADR/root-contract changes.
8. Named verdicts clear gates only from
   `reviews/<task-id>/<run-id>-<authority>.md` with `decision: approved`.

Use `.agent-work/production-readiness/` for task contracts, state, logs, and verification.
