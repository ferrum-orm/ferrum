# Command: Architecture Impact

Reusable prompt for assessing whether a change needs architecture review or touches an ADR.

## Use when

Before starting a change that might affect service boundaries, the IR contract, data models,
security gates, resolved contracts, reopened ADR-004, or any new ADR.

## Prompt

Assess the architecture impact of this proposed change for Ferrum:

1. **Boundary impact.** Does it move responsibility across the Python/Rust boundary, or change
   what crosses it (the IR)? IR shape/version changes are governed by ADR-002.
2. **ADR dependencies.** ADR-001/002/003/005/006 are resolved; ADR-004 is
   reopened. Stop if the change contradicts a resolved contract or pre-empts
   ADR-004/a new ADR, and surface it to the ChiefArchitect.
3. **Security surface.** SQL compilation, migration apply, errors/redaction,
   auth/secrets, RLS/admin GUCs, and schema selection require SecurityEngineer
   review and cannot be self-cleared.
4. **Data model.** Does it introduce or change persistence shape? Apply Schema Evolution (additive,
   backward-compatible by default) and produce a data model.
5. **Blast radius & scaling.** What breaks if this fails? What load/growth assumptions does it
   make? Use CAP/Blast Radius/Data Gravity lenses.
6. **Verdict.** Does this require architecture review before implementation? Yes/No, with reasons.

## Output

An impact assessment naming affected boundaries, ADR dependencies, security flags, data-model
impact, blast radius, and a clear yes/no on whether architecture review is required first.
