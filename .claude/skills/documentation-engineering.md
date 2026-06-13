# Skill: Documentation Engineering

> Expert behavior for keeping Ferrum's docs accurate and in lockstep with the public API. Use when
> a public API changes, when writing guides/reference, or when reconciling docs with the contract.

## When to use

- Any change to a public, importable API (docs update is mandatory in the same change).
- Writing or revising README, getting-started, reference, or conceptual docs.
- Reconciling docs against the PRD, architecture review, and security gates.

## Expert behaviors

- **Docs are part of the change, not a follow-up.** A public API change without a docs update in
  the same diff is incomplete (`AGENTS.md` §2/§8).
- **Actionable and self-contained.** A developer must understand and recover from errors without
  reading Ferrum source. Document failure modes, not just the happy path.
- **No secrets, ever.** Examples never include real credentials, DSNs, or production connection
  strings. Use obvious placeholders.
- **Match the committed API shape.** README is the external contract; keep examples runnable and
  consistent with the actual async, Pydantic-v2-native surface.
- **Encode the guarantees.** Where relevant, state the async-first, PostgreSQL-only,
  Tier-A-observability, and allowlist-safety guarantees so users build correct mental models.

## Workflow

1. Identify the public surface that changed and every doc that references it.
2. Update README + affected docs with runnable, sanitized examples.
3. Verify examples reflect real semantics (async, error taxonomy, observability tiers).
4. Cross-check against PRD/architecture review; flag any divergence rather than papering over it.

## Anti-patterns

- Deferring docs to "a later docs task" after a public API change.
- Example code with real secrets or production DSNs.
- Documenting behavior that contradicts the PRD/architecture contract.
- Sync-style examples that imply a non-existent synchronous API.
