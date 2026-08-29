---
task_id: w0-b-consumer-parity
run_id: 20260821T075801Z
authority: ChiefArchitect
reviewer: chief-architect
reviewed_at: 2026-08-21T09:20:00Z
base_revision: 768ec1f3013f6d0eccd7c8b590ba36b54b12d23e
decision: approved
scope:
  - Read-only Ticket Analyzer / Org AI Platform parity inventory (ta-01..ta-16, oai-01..oai-10)
  - Contract tests confined to tests/consumer_contracts/; no production EnableRLS or db_default allowlist changes
  - ADR-004 remains reopened; W0-A §5a retry/tenancy contracts are not reopened
  - ta-15 quoted-string DEFAULT allowlist defect (inventory only)
  - ta-16 EnableRLS(force=True) emits FORCE without ENABLE (inventory only; SecurityEngineer)
---

# Named Authority Verdict

## Authority

`ChiefArchitect`

Narrative label for this gate: **Aligned** (maps to `decision: approved`).

This record grants only the ChiefArchitect gate for run `20260821T075801Z`. It does not grant SecurityEngineer, ProductManager, or CodeReviewer clearance. It does not authorize Wave 1 implementation, EnableRLS production repair, or `_DEFAULT_VALUE_ALLOWLIST` expansion. It does not reopen `AGENTS.md` §5a.

## Claims reviewed

1. Every audited Ticket Analyzer (`ta-01`..`ta-16`) and Org AI Platform (`oai-01`..`oai-10`) persistence call path is classified (`supported` / `ferrum_defect` / `missing_ferrum_api` / `consumer_refactor`) with a verbatim source citation.
2. Contract tests stay inside `tests/consumer_contracts/` and do not pre-empt ADR-004 or W1 implementation.
3. `ta-15`: Ferrum `_DEFAULT_VALUE_ALLOWLIST` rejects quoted string literals such as `'pending'` used by Ticket Analyzer `Field(db_default="'pending'")`. Inventory records this as a Ferrum defect; Wave 0 must not broaden the allowlist.
4. `ta-16`: `EnableRLS(force=True)` emits FORCE without ENABLE. Inventory records it; production fix is out of this workstream. Flag SecurityEngineer; do not self-clear.

## Evidence

Inspected independently from source. The executor log
`.agent-work/production-readiness/logs/w0-b-consumer-parity/20260821T075801Z.md`
was treated as a claim list only. No production files were edited. `mise run ci-local` was not re-run (unowned `agent-orchestration` / W0-A state drift is out of this gate).

### Inventory and tests (owned path only)

- `tests/consumer_contracts/manifest.py` — 16 Ticket Analyzer + 10 Org AI Platform `ParityEntry` rows; pinned revisions `ae7e262865db5d0472132ff5171770568dc79ae0` (TA) and `561a46a1fe409d238068e02994e9c942b5cad706` (OAI).
- `tests/consumer_contracts/test_manifest_integrity.py` — unique ids, four-bucket classification, category coverage, whitespace-normalized excerpt presence in the cited consumer file.
- `tests/consumer_contracts/test_ticket_analyzer_contracts.py` — live-PG contracts plus a `strict=True` xfail for FORCE-without-ENABLE; schema helpers work around `ta-15`/`ta-16` in fixtures only.
- `tests/consumer_contracts/test_org_ai_platform_contracts.py` — current-state absence checks plus live-PG COALESCE-upsert and named-schema `detect_drift`.
- `tests/consumer_contracts/conftest.py` — self-contained `pg_conn` / `FERRUM_TEST_DSN`; does not import unowned `tests/python/integration/`.

Changed-path list in workstream state is confined to `tests/consumer_contracts/*`. No `python/`, `crates/`, `AGENTS.md`, or W0-A contract files in this run.

### Claim 1 — classifications and citations

Independent spot-check of cited consumer source (working trees at the listed paths; excerpts match after whitespace normalization):

| id | class | citation verified |
|---|---|---|
| ta-01 | supported | `packages/infra/src/infra/db/team_session.py` `set_config('app.team_id', ...)` |
| ta-02 | supported | same file, `app.platform_admin` |
| ta-03 | supported | `packages/domain/src/domain/ticket.py:62-66` composite `primary_key=True` on `id` and `first_seen_at` |
| ta-04 | supported | `webhook_events_crud.py:93-105` `filter` + `update_returning` CAS |
| ta-05 | supported | `_UNLOCKED` = `Q(locked_until__is_null=True) \| Q(locked_until__lt=now)` |
| ta-06 | supported | `alerts_crud.py:48-52` `slack_delivery__contains={"ok": False}` |
| ta-07 | supported | `alert.py:113-117` `list[UUID]` / `db_default="'{}'"` |
| ta-08 | supported | `tickets_crud.py:196-203` `nearest_to(..., metric="cosine")` |
| ta-09 | supported | `tickets_crud.py:264-270` `bulk_upsert` static `update_fields` |
| ta-10 | supported | `tickets_crud.py:157-159` `query.stream(...)` |
| ta-11 | supported | `retention_crud.py:21-26` `tx.call_function` |
| ta-12 | ferrum_defect | consumer already uses `__is_null`; live test shows `filter(x=None)` matches zero NULL rows |
| ta-13 | supported | `tickets_crud.py:171-186` `aggregate_tickets` / `group_by` |
| ta-14 | missing_ferrum_api | `llm_provider_credentials_crud.py:117-136` raw `text()` upsert of `bytea` ciphertext |
| ta-15 | ferrum_defect | `webhook_event.py:35-38` `Field(db_default="'pending'", ...)` |
| ta-16 | ferrum_defect | `migrations/0018-force-rls.sql:3-20` ENABLE-then-FORCE defense in depth |
| oai-01 | missing_ferrum_api | `async_sql_engine.py:195-200` `schema_translate_map` |
| oai-02 | missing_ferrum_api | `tenant_shard.py:29-39` catalog upsert |
| oai-03 | missing_ferrum_api | `task_utils.py:71-77` `with_for_update(skip_locked=True)` |
| oai-04 | missing_ferrum_api | `document.py:1370-1373` `with_for_update(nowait=True)` |
| oai-05 | consumer_refactor | `document_set.py:621-635` two-hop `.join()`; Ferrum one-level `a__b` is the bound contract |
| oai-06 | missing_ferrum_api | `pydantic_type.py:9-10`; `models.py:868` `_SUPPORTED_TYPES.get(base_type, "text")` |
| oai-07 | missing_ferrum_api | `entities.py:124-136` COALESCE / `||` / additive SET |
| oai-08 | supported | `encrypted_kv_store.py:21-26` static excluded-value upsert |
| oai-09 | missing_ferrum_api | `users.py:2036-2038` FastAPI-Users SQLAlchemy adapter |
| oai-10 | supported | `alembic/env.py:255-263` per-schema drift compare; Ferrum `detect_drift(..., schema=)` |

All four classification buckets are populated. Boundary: Python-side inventory of public APIs and migration DDL; no IR version bump; Rust stays off this workstream.

### Claim 2 — no ADR-004 / W1 pre-emption

- Tests call existing `ferrum.migrations.apply()` (autocommit-per-op). They do not wrap apply in a migration-spanning transaction, advisory lock, or atomic ledger write. ADR-004 stays reopened (`AGENTS.md` §5); W1-C remains owner.
- `oai-01` / `oai-02` `MISSING_API` matches **current** code (no `schema_transaction` / `ConnectionRegistry` / `ShardRouter` under `python/ferrum/`). That is the ratified W0-A §5a *implementation gap*, not a reversal. W1-F still owns those APIs. Absence tests are a **current-state snapshot**, not a prohibition.
- `oai-03` / `oai-04` `MISSING_API` matches current `QuerySet` (no `select_for_update`). The approved plan W1-B still owns `select_for_update(nowait=..., skip_locked=..., of=...)`. Inventory notes that Ticket Analyzer CAS is a viable *lease-claim* substitute; that note must not cancel W1-B. `oai-04` `nowait=True` is fail-fast lock contention, not CAS-equivalent (Least Astonishment).
- `oai-05` as `consumer_refactor` honors the existing one-level relation-lookup rejection. Do not grow nested `a__b__c` hops in Wave 0 (YAGNI).
- Fixture workaround `EnableRLS(table)` then `EnableRLS(table, force=True)` is test-only. It must not become a documented public substitute for a correct `force=True` emitter.

### Claim 3 — ta-15 allowlist (do not broaden in Wave 0)

Ferrum source (`python/ferrum/migrations/orchestrator.py:199-214`, checks at `:409` and `:520`):

`_DEFAULT_VALUE_ALLOWLIST` = `{NULL, TRUE, FALSE, NOW(), CURRENT_TIMESTAMP, CURRENT_DATE, CURRENT_TIME, GEN_RANDOM_UUID(), UUIDV7(), 0, 1, ''}`.

`_python_default_to_sql` (`:230-233`) already refuses non-empty Python strings as SQL DEFAULT. Ticket Analyzer `WebhookEvent.status` uses `Field(db_default="'pending'")`. Classification `ferrum_defect` is correct. This run does not change the allowlist.

**Future-fix constraint (not approved as an implementation here):** the manifest note “allow any single-quoted literal” is **not** an architecture decision. Broadening interpolated DEFAULT tokens is SQL-compilation / migration-apply (AGENTS.md §2.9, §3). Any later design must fail closed on embedded quotes/escapes and go through SecurityEngineer. Wave 0 must not ship it.

Related residual (not a missing W0-B row): `Alert.ticket_ids` `db_default="'{}'"` and `slack_delivery` `db_default="'{}'::jsonb"` hit the same allowlist class. `ta-15` is the representative defect; `ta-07` correctly classifies uuid[] *round-trip*, not the DEFAULT token.

### Claim 4 — ta-16 FORCE without ENABLE

`EnableRLS.to_op_dict` sets `force` (`operations.py:475-480`). Orchestrator (`orchestrator.py:637-641`):

```
if kind == "enable_rls":
 if op.get("force"):
 return "... FORCE ROW LEVEL SECURITY"
 return "... ENABLE ROW LEVEL SECURITY"
```

`force=True` replaces ENABLE rather than pairing it. PostgreSQL: `FORCE` without `ENABLE` leaves `relrowsecurity` false; policies are a silent no-op (Blast Radius: complete RLS bypass, not merely owner bypass). Ticket Analyzer `0018-force-rls.sql` applies FORCE on tables that already have ENABLE — the consumer shape a Ferrum-migrated `EnableRLS(..., force=True)` must reproduce.

The `strict=True` xfail
`test_force_rls_without_enable_rls_grants_no_isolation_defect`
records the defect without shipping a production fix. Correct W0-B posture.

`operations.py:470-472` docstring currently implies FORCE is what `force=True` emits (replacement semantics). Repair belongs with the production fix, not this inventory.

**This finding is not self-cleared.** SecurityEngineer review is required before any orchestrator/ops change.

## Findings

### Blocking

None for this workstream’s owned scope.

### Non-blocking

1. **Absence tests vs Wave 1 (Schema Evolution).** `test_schema_per_tenant_routing_is_not_available_missing_api`, `test_shard_router_registry_is_not_available_missing_api`, and `test_select_for_update_skip_locked_is_not_available_missing_api` assert APIs **must not exist**. They snapshot today’s tree. W1-B / W1-F **must retire or invert** them when shipping the ratified APIs. They do not freeze W0-A §5a or cancel W1-B `select_for_update`.
2. **oai-03 notes overreach.** CAS/`update_returning` is the Ticket Analyzer lease pattern (`ta-04`). It is not a substitute for `nowait` (`oai-04`) or for the planned W1-B lock primitive. Treat the note as consumer-migration commentary, not an ADR.
3. **ta-15 notes vs SQL safety.** Do not treat “any single-quoted literal” as the approved fix. Fail-closed quoting + SecurityEngineer when W1-C / W3 (or ops owner) implements. Related `'{}'` / `'{}'::jsonb` defaults are the same defect class.
4. **Comment id mixup (CodeReviewer).** `test_force_rls_without_enable_rls_grants_no_isolation_defect` docstring/xfail reason and `_apply_rls_schema` comments label the RLS defect `ta-15`; the manifest id is `ta-16`. Does not change classification.
5. **Citation integrity is machine-bound (CodeReviewer / W0-C).** `test_manifest_integrity.py` hard-codes `/Users/guyshaked/Desktop/dev/repos/{ticket-analyzer-agent,org-ai-platform}` and is unmarked, so default pytest addopts will fail where those checkouts are absent. Skip-if-missing or fixture the roots; do not block this architecture gate.
6. **Wide line ranges.** `ta-09` `1-300` and `ta-10` `1-340` still contain the excerpts; CodeReviewer may tighten. Architecture claim (real call path) holds.
7. **oai-06 silent TEXT fallback.** Unrecognized annotations mapping to TEXT (`models.py:868`) is Least Astonishment debt for W2-A (`FieldCodec` / nested Pydantic JSONB). Inventory as `missing_ferrum_api` is acceptable; fail-fast at class definition is the later contract, not a Wave 0 implementation.

## Decision

`approved`

W0-B is a read-only, source-cited parity inventory plus executable contracts under `tests/consumer_contracts/`. It does not move responsibility across the Python/Rust boundary, does not close ADR-004, does not reopen W0-A §5a, and does not ship EnableRLS or DEFAULT-allowlist production fixes.

## Required doc/plan edits

None before this inventory is accepted.

Do **not** edit `AGENTS.md` §5a. Do **not** start Wave 1 from this verdict.

Later owners (not this run):

- W1-B: `select_for_update`; retire W0-B absence tests when the API ships.
- W1-C (migration apply / ops SQL): EnableRLS ENABLE+FORCE pairing **after** SecurityEngineer approval; ADR-004 transactionality remains separate.
- W1-F: `schema_transaction` / `platform_admin_transaction` / `ConnectionRegistry` / `ShardRouter` per ratified §5a; retire oai-01/oai-02 absence tests then.
- W2-A: nested Pydantic JSONB, encrypted codecs (`ta-14`, `oai-06`, `oai-07` expression upsert as a distinct missing surface).
- DEFAULT string literals (`ta-15`): not Wave 0; SecurityEngineer on any allowlist/emitter change.

## Escalations

- **SecurityEngineer (required, not self-cleared):** `ta-16` `EnableRLS(force=True)` emits FORCE without ENABLE — silent RLS no-op (`AGENTS.md` §3 migration-apply / tenancy). Also any future `_DEFAULT_VALUE_ALLOWLIST` / quoted-DEFAULT emitter change (`ta-15`).
- **CodeReviewer:** test id mixup (`ta-15` vs `ta-16` comments), hardcoded consumer-repo paths, absence-test snapshot comments.
- **ProductManager:** not required for this inventory.
- **ProductDesigner / CEO:** none.
