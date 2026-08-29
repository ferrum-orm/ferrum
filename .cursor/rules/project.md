# Rule: Project — Ferrum

`AGENTS.md` is the authoritative architecture, security, ADR, capability, and
working contract. Read it before substantial work; do not cache its contents here.

Current supporting sources:

- `README.md` — public API and product positioning.
- `CHANGELOG.md` — shipped behavior and compatibility history.
- `.cursor/plans/ferrum-production-readiness_6b5f422d.plan.md` — approved
  production-readiness scope and dependencies.
- Source and tests — authority for current implementation behavior.

ADR-004 is reopened until migration execution satisfies its transactional contract.
Other original ADRs are resolved as recorded in `AGENTS.md` §5.

For production-readiness work, load
`.agent-work/production-readiness/PROTOCOL.md`. Preserve async-native/Pydantic-first
boundaries, bound values, allowlisted identifiers, Tier-A redaction, explicit
connections, tests with behavior, and docs with public API changes.
