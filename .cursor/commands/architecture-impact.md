# Command: Architecture Impact

Reusable prompt for assessing whether a change needs architecture review or touches an ADR.

## Use when

Before starting a change that might affect service boundaries, the IR contract, data models,
security gates, or any open ADR.

## Prompt

Assess the architecture impact of this proposed change for Ferrum:

1. **Boundary impact.** Does it move responsibility across the Python/Rust boundary, or change
   what crosses it (the IR)? IR shape/version changes are governed by ADR-002.
2. **ADR dependencies.** Does it depend on or pre-empt any open ADR?
   - ADR-001 driver placement · ADR-002 IR contract · ADR-003 hydration semantics ·
     ADR-004 migration transactionality · ADR-005 packaging/CI matrix · ADR-006 error/hook layer.
   If it pre-empts an undecided ADR, stop and surface it to the ChiefArchitect.
3. **Security surface.** Does it touch auth, secrets, SQL compilation, or migration apply? If so,
   it must be flagged for SecurityEngineer review and cannot be self-cleared.
4. **Data model.** Does it introduce or change persistence shape? Apply Schema Evolution (additive,
   backward-compatible by default) and produce a data model.
5. **Blast radius & scaling.** What breaks if this fails? What load/growth assumptions does it
   make? Use CAP/Blast Radius/Data Gravity lenses.
6. **Verdict.** Does this require architecture review before implementation? Yes/No, with reasons.

## Output

An impact assessment naming affected boundaries, ADR dependencies, security flags, data-model
impact, blast radius, and a clear yes/no on whether architecture review is required first.
