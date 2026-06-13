# Command: Update Docs

Reusable prompt for updating documentation alongside a public API change.

## Use when

A public, importable API changed, or docs have drifted from the committed contract.

## Prompt

Update Ferrum's documentation for this change. Requirements:

1. **Same-change rule.** A public API change is incomplete without a docs update in the same diff.
   Identify every doc (README + others) that references the changed surface.
2. **Runnable, sanitized examples.** Examples reflect the real async, Pydantic-v2-native surface.
   No real credentials, DSNs, or production connection strings — use obvious placeholders.
3. **Document failure modes.** Show the relevant Ferrum exception(s) and how to recover, not just
   the happy path. Errors must be understandable without reading source.
4. **Encode guarantees where relevant.** Async-first, PostgreSQL-only, Tier-A observability
   default, allowlist SQL safety.
5. **Reconcile with the contract.** Cross-check against the PRD and architecture review; if docs
   would have to contradict them, stop and flag the divergence.

## Output

Updated docs with accurate, runnable, secret-free examples consistent with the committed API and
the product/architecture contract.
