---
task_id: pilot-org-ai-platform
run_id: 20260829T104025Z
authority: ChiefArchitect
reviewer: chief-architect-agent
reviewed_at: 2026-08-29T10:47:02Z
base_revision: 768784ef788eb0641c224ead99d1e35662c3f8e3
decision: approved
scope:
  - Contract architecture for retargeted Org AI Platform consumer pilot tests
  - Retargeted tests verify correct behavior of schema_transaction, ShardRouter, select_for_update
  - Validation tests cover schema/shard/codec/lock APIs against ratified W1-F/W1-B/W2-A contracts
---

# Named Authority Verdict

## Authority

ChiefArchitect.

## Claims reviewed

1. The three stale "missing API" tests are retargeted to verify the APIs
   exist and work, consistent with the ratified W1-F (schema tenancy /
   sharding), W1-B (select_for_update), and W2-A (encrypted codecs)
   contracts in AGENTS.md §5a / §10.
2. The retargeted tests verify the **correct** architectural behavior, not
   merely existence: schema_transaction validates identifiers structurally
   (regex + allowlist); ShardRouter resolves trusted keys to
   connection-explicit QuerySet (shard-unaware QuerySet); select_for_update
   fails fast on mutually exclusive modifiers.
3. Validation tests cover the schema, shard, codec, and row-lock API
   surfaces required by the task contract.
4. No architectural contract in §2 / §5a is preempted or violated by the
   test changes.

## Evidence

- `git diff HEAD -- tests/consumer_contracts/test_org_ai_platform_contracts.py`
  — full diff inspected. 18 tests (3 retargeted existence + 11 new
  validation + 3 preserved defect/supported proofs + 1 companion
  compile-rejection).
- `python/ferrum/session.py:219-241,294-344` — `_validate_schema_name`
  enforces regex THEN allowlist; `schema_transaction` opens a pinned
  transaction and binds `search_path` via `set_config(..., true)`
  (transaction-local reset). Matches the §5a contract: "validated schema
  selection on one pinned transaction, not implicit routing."
- `python/ferrum/routing.py:55-63,156-186,293-346` —
  `_ensure_postgres_dsn` rejects non-postgres at registration
  (structural, not by convention); `ConnectionRegistry.get` raises when
  closed or unregistered; `ShardRouter.connection_for` uses a caller
  resolver (trusted key) and returns an explicit `Connection`. Matches
  §5a: "QuerySet stays shard-unaware and connection-explicit... No
  implicit connection selection from model metadata, tenant id, or
  schema name."
- `python/ferrum/queryset.py:1789-1840` — `select_for_update` rejects
  `nowait=True, skip_locked=True` at compile time before SQL emission
  (§2.9 / §3 SQL safety).
- `python/ferrum/models.py:688-760,897-995` — encrypt-then-MAC with random
  nonce; `KeyProvider` protocol injection; `redact()` omits plaintext/key
  bytes.
- Fresh test run:
  `FERRUM_TEST_DSN=... uv run pytest tests/consumer_contracts/test_org_ai_platform_contracts.py -q -m ""`
  → `18 passed in 0.34s` (exit 0).
- `grep -rn "xfail" tests/` → zero matches.

## Findings

- **Low — `.bench-results/perf.json` side-effect:** the executor's
  `mise run ci-local` regenerated this tracked benchmark artifact
  (deterministic side-effect of `test_performance.py` with
  `FERRUM_BENCH_RECORD`). Not an architectural concern; revert before
  commit. The executor log's "Changed paths" section omitted it.
- **Low — executor `finished_at` timestamp inconsistency:** log claims
  11:35:00Z finish but verifier wall clock is 10:47:02Z. Work product is
  real; log-hygiene only.
- No architecture findings. The retargeted tests correctly verify the
  ratified contracts. The preserved oai-06 / oai-07 / oai-10 defect proofs
  are correctly retained as distinct gap proofs (not retargeted), which
  preserves the consumer-migration gap record without claiming those are
  fixed.

## Decision

**approved.** The contract architecture is sound. Retargeted tests verify
the correct architectural behavior of `schema_transaction`,
`ShardRouter`/`ConnectionRegistry`, and `select_for_update` against the
ratified W1-F / W1-B contracts. Validation tests cover schema (allowlist
+ search_path reset + injection rejection), shard (trusted keys +
connection-explicit + PostgreSQL-only), codec (key-provider + PII
redaction), and row-lock (nowait/skip_locked) APIs. No §2 / §5a contract
is preempted or violated.

This record grants only the ChiefArchitect gate. It does not substitute
for another authority or independent verification.
