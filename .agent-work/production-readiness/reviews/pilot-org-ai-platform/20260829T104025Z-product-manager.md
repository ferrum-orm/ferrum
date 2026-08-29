---
task_id: pilot-org-ai-platform
run_id: 20260829T104025Z
authority: ProductManager
reviewer: product-manager-agent
reviewed_at: 2026-08-29T10:47:02Z
base_revision: 768784ef788eb0641c224ead99d1e35662c3f8e3
decision: approved
scope:
  - Consumer readiness assessment for Org AI Platform (Onyx-fork) pilot
  - Whether the Org AI Platform contracts are satisfied
  - What remains for actual consumer migration
  - Acceptability of remaining defect items (oai-06, oai-07)
---

# Named Authority Verdict

## Authority

ProductManager.

## Claims reviewed

1. The Org AI Platform consumer-pilot contract tests are retargeted and
   validated against the now-complete Ferrum implementation, satisfying
   the in-repo contract-validation scope of this pilot.
2. The remaining defect items (oai-06 nested Pydantic → TEXT fallback,
   oai-07 COALESCE-based bulk_upsert) are acceptable as documented
   consumer-migration gaps and do not block this pilot's acceptance.
3. The actual consumer codebase migration (refactoring Org AI Platform to
   async Ferrum) remains out of scope for this tick (external work).

## Evidence

- Task contract (`tasks/pilot-org-ai-platform.md`) scope: "scoped to
  IN-REPO contract validation only. Actual consumer codebase migration is
  EXTERNAL work requiring access to the consumer repository and is out of
  scope for this tick."
- Test inventory (`test_org_ai_platform_contracts.py`, 18 tests):
  - oai-01 schema-per-tenant: retargeted + 3 validation tests (allowlist,
    search_path reset, injection rejection). ✓
  - oai-02 shard routing: retargeted + 1 validation test (trusted keys,
    connection-explicit, PostgreSQL-only). ✓
  - oai-03/oai-04 SELECT FOR UPDATE [SKIP LOCKED|NOWAIT]: retargeted + 3
    validation tests (compile rejection, skip_locked, nowait). ✓
  - oai-06 nested Pydantic → TEXT: **preserved as defect proof** (line
    497). W2-A codec-registry gap; nested Pydantic BaseModel annotation
    silently falls back to TEXT (wrong DDL type). Test asserts the gap
    remains. ✓ (documented, not fixed)
  - oai-07 COALESCE-based bulk_upsert: **preserved as defect proof**
    (line 518, integration). `bulk_upsert` static `update_fields` cannot
    express COALESCE — a new row's `None` overwrites an existing non-null.
    Test asserts the gap remains. ✓ (documented, not fixed)
  - oai-10 schema-scoped drift detection: preserved as supported-behavior
    proof (line 584, integration). ✓
- Encrypted/JSON codec contracts (oai-07/oai-08 codec): 5 validation tests
  covering key-provider injection, non-deterministic encryption, MAC
  failure, factory key-provider requirement, PII redaction. ✓
- Fresh test run: `18 passed` in file, `88 passed` across
  `tests/consumer_contracts/` (both pilots + manifest integrity).
- Compatibility policy (§5a): Ferrum is alpha (0.x); no semver/stability
  contract before 1.0. The retargeted tests verify the public Python API
  surface (`ferrum.schema_transaction`, `ferrum.ShardRouter`,
  `QuerySet.select_for_update`) which is the documented public surface in
  §10 / README. No IR or SQL-text contract is being asserted (correct —
  those are not stability surfaces).

## Findings

### Consumer readiness assessment

The in-repo contract validation scope of this pilot is **satisfied**:
the three previously-"missing" APIs that blocked Org AI Platform's
persistence patterns (schema-per-tenant routing, shard registry,
SELECT FOR UPDATE SKIP LOCKED/NOWAIT) are now implemented and proven by
retargeted tests against live PostgreSQL. The encrypted-codec contracts
(key-provider injection, PII redaction) needed for Org AI Platform's
encrypted-at-rest fields are validated.

### What remains for actual consumer migration (EXTERNAL, out of scope)

This pilot proves Ferrum **can** support the Org AI Platform persistence
patterns; it does NOT migrate the consumer codebase. Remaining external
work (requires access to the Org AI Platform repository, not Ferrum):

1. Refactor Onyx's `schema_translate_map` usages to `schema_transaction`
   with an explicit tenant-schema allowlist registered at app startup.
2. Replace Onyx's multi-DB session/shard logic with `ConnectionRegistry` +
   `ShardRouter` (caller-supplied resolver for trusted shard keys).
3. Replace `.with_for_update(skip_locked=True)` / `.with_for_update(nowait=True)`
   with `QuerySet.select_for_update(skip_locked=True)` / `(nowait=True)`.
4. Wire encrypted fields to `EncryptedStringCodec` / `EncryptedJSONCodec`
   with a production `KeyProvider` (KMS-backed, not the test
   `_StaticKeyProvider`).
5. Resolve oai-06: either map nested Pydantic BaseModel annotations to
   JSONB explicitly (W2-A codec-registry extension) or use a
   `NestedModelCodec`-style explicit field declaration in the consumer
   model.
6. Resolve oai-07: either express COALESCE-based conditional upsert via a
   stored procedure (`call_function`) or a Ferrum SQL-expression SET
   clause extension (not yet shipped), or restructure the consumer's
   `transfer_entity` logic to avoid the COALESCE-on-upsert pattern.

### Acceptability of remaining defect items (oai-06, oai-07)

- **oai-06 (nested Pydantic → TEXT fallback):** Acceptable as a documented
  gap. It is a wrong-DDL-type defect (silent fallback, not a clean
  rejection), but it does not block the core persistence patterns and has
  a consumer-side workaround (explicit JSONB field / NestedModelCodec
  declaration). It is correctly preserved as a defect proof so it is not
  silently lost. Recommend tracking as a follow-up W2-A codec-registry
  enhancement.
- **oai-07 (COALESCE-based bulk_upsert):** Acceptable as a documented
  gap. `bulk_upsert` with a static `update_fields` list is a shipped,
  supported API; the gap is the inability to express SQL-expression SET
  clauses (COALESCE). The consumer's `transfer_entity` pattern has
  alternatives (stored procedure via `call_function`, or application-level
  conditional logic). Correctly preserved as a defect proof. Recommend
  tracking as a follow-up query-expressiveness enhancement.

Neither defect item is a regression introduced by this pilot, and both
are correctly retained as proof-of-gap rather than hidden.

### Product notes

- The parallel `pilot-ticket-analyzer` run was mid-edit on `manifest.py`
  during this run (transient syntax error, fixed by the parallel run).
  This is a coordination concern for parallel pilots sharing
  `tests/consumer_contracts/`, not a product defect in this pilot. Future
  parallel pilots should serialize the shared `manifest.py` path.
- `.bench-results/perf.json` was regenerated as a ci-local side-effect
  (benchmark artifact, not a product/contract change). Revert before
  commit.

## Decision

**approved.** The in-repo consumer-pilot contract validation scope is
satisfied. The Org AI Platform contracts are retargeted and validated
against the implemented Ferrum APIs (schema_transaction, ShardRouter,
select_for_update, encrypted codecs). The remaining defect items
(oai-06, oai-07) are acceptable as documented consumer-migration gaps
with identified workarounds; they are correctly preserved as defect
proofs. Actual consumer codebase migration remains external, out-of-scope
work.

This record grants only the ProductManager gate. It does not substitute
for another authority or independent verification.
