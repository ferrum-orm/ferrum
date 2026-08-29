---
task_id: pilot-ticket-analyzer
run_id: 20260829T104025Z
authority: ChiefArchitect
reviewer: chief-architect-agent
reviewed_at: 2026-08-29T11:35:00Z
base_revision: 768784ef788eb0641c224ead99d1e35662c3f8e3
decision: approved
scope:
  - tests/consumer_contracts/test_ticket_analyzer_contracts.py
  - tests/consumer_contracts/manifest.py
  - tests/consumer_contracts/conftest.py
---

# Named Authority Verdict

## Authority

ChiefArchitect

## Claims reviewed

1. Retargeted ta-12 test verifies the correct behavior: `filter(x=None)`
   compiles to `IS NULL` (Django-parity), not `= NULL` (defect).
2. Manifest classifications are architecturally accurate for all 8
   reclassified entries.
3. The 3 remaining items (ta-15, oai-05, oai-07) are correctly classified
   as not-yet-supported, consistent with ratified W0-A/W1-F contracts.
4. OAI entry evidence correctly delegates contract-test validation to
   pilot-org-ai-platform (parallel pilot owns `test_org_ai_platform_contracts.py`).
5. No architectural invariants (§2) violated by the contract changes.

## Evidence

**ta-12 retarget**: Source read of `python/ferrum/queryset.py:438-466`
confirms `_normalize_null_lookup` rewrites `operator="eq"` + `value=None` →
`("is_null", None)` and `operator="ne"` + `value=None` → `("is_not_null", None)`.
The retargeted test `test_filter_equals_none_matches_null_rows_django_parity`
asserts the fixed behavior: `filter(x=None)` finds the NULL row, and
`exclude(x=None)` finds the non-NULL row. This is the correct Django-parity
semantics the original defect note recommended.

**Manifest classifications**: Each reclassified entry's `ferrum_reference`
and `evidence` fields cite direct source reads of the implementing workstream:
- oai-01 → `ferrum.session.schema_transaction` (ratified W1-F §5a contract)
- oai-02 → `ferrum.routing.ConnectionRegistry`/`ShardRouter` (ratified W1-F)
- oai-03/04 → `QuerySet.select_for_update` (W1-B)
- oai-06 → `NestedModelCodec` (W2-A)
- oai-09 → `FerrumUserDatabase` (W2-D)
- ta-14 → `EncryptedStringCodec`/`EncryptedJSONCodec` (W2-A)

All are consistent with the architecture: QuerySet stays connection-explicit
(§2, §5a), no implicit multi-DB, no sync wrappers.

**Remaining items**: ta-15 (migration default string literal) remains
FERRUM_DEFECT — `_DEFAULT_VALUE_ALLOWLIST` in `orchestrator.py` still only
allows `''`. oai-05 (multi-hop relation filters) remains CONSUMER_REFACTOR —
one-level lookups are the ratified design (§5a "nested hops rejected"). oai-07
(expression-based upsert SET) remains MISSING_API. These classifications are
architecturally accurate and do not violate any ratified contract.

**OAI delegation**: The manifest evidence for oai-01/02/03/04/06/09 states
"Contract test validation is owned by pilot-org-ai-platform." This is correct:
`test_org_ai_platform_contracts.py` is an unowned path for this workstream
(modified by the parallel pilot). The TA manifest entries cite the source
implementation, not a missing test, which is architecturally sound.

## Findings

No blocking findings.

Minor observation (non-blocking): The ta-14 manifest entry offers two
migration paths (EncryptedStringCodec vs plain bytes field). Both are
architecturally valid; the consumer chooses based on whether they want
transparent encryption or to keep pre-encryption outside the ORM. This
is consistent with §2 (Pydantic v2 first, no duplicate schemas).

## Decision

**approved** — The contract architecture is sound. Retargeted tests verify
correct behavior. Manifest classifications are architecturally accurate and
consistent with ratified W0-A/W1-F contracts. No architectural invariants
violated.
