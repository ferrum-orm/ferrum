# Decisions Log (ADR Index) Template

> Running index of Ferrum architecture decisions. Owner: ChiefArchitect. The architecture
> feasibility review enumerates six ADRs that must be resolved before the relevant implementation
> begins. Until an ADR is decided, do not hard-code a choice that forecloses it.

## How to use

- One entry per decision. Use the ADR template below. Link decisions from the relevant
  ARCH/QUERY_ENGINE/MIGRATIONS/DATA_MODELING plan.
- Status: `proposed` → `accepted` / `rejected` / `superseded`.

## Open ADRs (must be resolved before relevant implementation)

| ADR | Topic | Status | Notes |
| --- | --- | --- | --- |
| ADR-001 | PostgreSQL driver placement | proposed | `asyncpg` Python-side is the default leaning |
| ADR-002 | QuerySet→Rust IR contract shape & version stability | proposed | Identifiers/values out-of-band |
| ADR-003 | Hydration semantics | proposed | construct-without-revalidate vs full validation |
| ADR-004 | Migration transactionality + non-transactional exception list | proposed | `CREATE INDEX CONCURRENTLY`, some `ALTER TYPE`/enum |
| ADR-005 | Packaging targets & CI wheel matrix | proposed | maturin + cibuildwheel, abi3 |
| ADR-006 | Centralized error-mapping + hook-payload/redaction layer | proposed | Non-bypassable |

## ADR entry template

### ADR-NNN: <Title>

- **Status:** proposed | accepted | rejected | superseded (by ADR-XXX)
- **Date:**
- **Decision owner:** ChiefArchitect
- **Context:** the forces and constraints (cite PRD/architecture review).
- **Decision:** the choice made.
- **Alternatives considered:** each option and why rejected (cite design lenses by name).
- **Consequences:** tradeoffs, blast radius, what this enables/forecloses.
- **Security/observability impact:** flag if it touches auth/secrets/SQL/migration (notify
  SecurityEngineer).
- **Affects:** linked plans/components.
