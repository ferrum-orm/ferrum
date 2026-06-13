# Architecture Template — <Feature / System>

> Template for a Ferrum architecture proposal. Owner: ChiefArchitect. A proposal is not done
> without a context diagram, component breakdown, data model, and tradeoff rationale.

## 1. Context diagram

Show the system and its external dependencies (PostgreSQL, the async driver, the calling
service). ASCII or fenced diagram acceptable.

```
[ App (FastAPI/Starlette) ] -> [ Ferrum Python (async, models, QuerySet, pool, hooks) ]
                                      | IR (typed, versioned)        ^ hydrated payload
                                      v                              |
                               [ Rust core: compile + hydrate ] -> [ PostgreSQL ]
```

## 2. Components & responsibilities

For each component: single responsibility, what it owns, what it must not own. Keep the
Python/Rust boundary explicit.

## 3. Data flow & state ownership

- What crosses the boundary (IR out, rows/payload back).
- Where state lives. **No per-request mutable state in Rust.**

## 4. Data model

Required for any new persistence. Tables/fields/types, keys, indexes, and Schema-Evolution plan
(additive, backward-compatible by default).

## 5. API contracts / interfaces

- Public async API signatures.
- The IR contract delta (governed by ADR-002).
- Integration contracts with the driver/PostgreSQL.

## 6. Scaling assumptions

- Expected load and growth curve; pool sizing; hot paths (compile, hydrate). CAP/Data Gravity
  tradeoffs made explicit.

## 7. Failure modes & observability

- Blast radius of each failure; cancellation/timeout behavior; Tier A signals required before
  launch.

## 8. Security requirements

- Explicit list engineers must implement (SQL safety, redaction, observability tiers, error
  taxonomy, migration guards). Flag auth/secrets/SQL/migration items for SecurityEngineer.

## 9. Alternatives considered

- Each alternative, why rejected, tradeoff rationale (cite design lenses by name).

## 10. ADR dependencies & decisions

- Which of ADR-001..006 this depends on; any decision recorded in `DECISIONS.md`.

## 11. Rollout & what engineers need to start

- Sequence, handoff to Backend/Frontend Engineers, and the first implementable slice.
