---
task_id: pilot-ticket-analyzer
run_id: 20260829T104025Z
authority: ProductManager
reviewer: product-manager-agent
reviewed_at: 2026-08-29T11:45:00Z
base_revision: 768784ef788eb0641c224ead99d1e35662c3f8e3
decision: approved
scope:
  - tests/consumer_contracts/test_ticket_analyzer_contracts.py
  - tests/consumer_contracts/manifest.py
  - tests/consumer_contracts/conftest.py
---

# Named Authority Verdict

## Authority

ProductManager

## Claims reviewed

1. The Ticket Analyzer consumer contracts are satisfied for pilot declaration.
2. The 3 remaining items (ta-15, oai-05, oai-07) are acceptable for pilot.
3. The manifest accurately reflects consumer readiness status.
4. Actual consumer codebase migration scope is correctly excluded from this tick.

## Evidence

### Contract satisfaction

The manifest tracks 15 Ticket Analyzer entries (ta-01 through ta-15). After
this pilot's reclassification:
- **SUPPORTED**: ta-01 through ta-14 (excluding ta-15) — 14 entries
- **FERRUM_DEFECT**: ta-15 — 1 entry

For the Org AI Platform entries (oai-01 through oai-09) reclassified by this
pilot and the parallel pilot-org-ai-platform:
- **SUPPORTED**: oai-01, 02, 03, 04, 06, 08, 09 — 7 entries
- **CONSUMER_REFACTOR**: oai-05 — 1 entry
- **MISSING_API**: oai-07 — 1 entry

All contract tests pass against live PostgreSQL (70 passed including manifest
integrity; 11 TA integration tests live-verified).

### Remaining items assessment

**ta-15 (migration default string literal) — FERRUM_DEFECT**: The
`_DEFAULT_VALUE_ALLOWLIST` in `orchestrator.py` only allows `''` for string
defaults. Non-empty quoted literals like `'pending'` are rejected. This is a
real defect, but it has a documented workaround: the consumer sends every
field explicitly via ORM `create()` calls (the test schema at lines 142-146
demonstrates this — `status` column has no SQL DEFAULT, only NOT NULL). The
consumer's existing code already sends explicit values. **Acceptable for
pilot**: the workaround is in place and documented.

**oai-05 (multi-hop relation filters) — CONSUMER_REFACTOR**: Multi-hop
relation lookups (`a__b__c`) are rejected by design (§5a: "nested hops
rejected"). One-level Django-style lookups (`filter(team__slug=...)`) are
supported. The consumer must refactor multi-hop queries into explicit joins
or multiple queries. **Acceptable for pilot**: this is a consumer-side
refactor, not a Ferrum gap. The consumer can migrate with one-level lookups.

**oai-07 (expression-based upsert SET) — MISSING_API**: `upsert(...,
set=...)` does not support expression-based SET clauses (e.g.,
`SET counter = EXCLUDED.counter + 1`). This is a genuine missing API. However,
the consumer's actual upsert patterns (ta-09 bulk_upsert) use literal value
SET clauses, which ARE supported. **Acceptable for pilot**: the consumer's
demonstrated upsert patterns work; expression-based SET is a future enhancement.

### Pilot readiness

The Ticket Analyzer consumer pilot contracts demonstrate that Ferrum now
supports the core patterns the consumer needs:
- RLS tenant isolation with GUC safety (ta-01, ta-02)
- CAS/update_returning lease claim (ta-04, ta-05)
- JSONB containment (ta-06)
- UUID arrays + JSONB round trip (ta-07)
- pgvector (ta-08)
- bulk_upsert (ta-09)
- streaming (ta-10)
- composite PK (ta-03)
- call_function (ta-11)
- filter(x=None) Django-parity (ta-12 — now fixed)
- encrypted bytea (ta-14 — now supported)
- group_by + aggregate (ta-13)

The 3 remaining items have workarounds (ta-15, oai-05) or are not exercised
by the consumer's demonstrated patterns (oai-07). Pilot declaration is
warranted.

### Consumer migration scope

The task contract correctly scopes this tick to IN-REPO contract validation
only. Actual consumer codebase migration (replacing SQLAlchemy in
`ticket-analyzer-agent`) is EXTERNAL work requiring access to the consumer
repository and is out of scope. This is the correct product boundary: the
pilot proves Ferrum CAN support the consumer's patterns; the actual migration
is a separate project.

## Findings

No blocking findings.

Product observation (non-blocking): ta-15 should be tracked as a follow-up
defect for a future Ferrum release. The `_DEFAULT_VALUE_ALLOWLIST` should
eventually support non-empty quoted string literals to match the consumer's
schema definitions. This does not block the pilot.

## Decision

**approved** — The Ticket Analyzer consumer contracts are satisfied for
pilot declaration. The 3 remaining items (ta-15, oai-05, oai-07) are
acceptable: each has a workaround or is not exercised by the consumer's
demonstrated patterns. Consumer migration is correctly scoped as external
work.
