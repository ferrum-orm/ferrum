---
task_id: w0-b-consumer-parity
run_id: 20260821T075801Z
authority: SecurityEngineer
reviewer: security-engineer
reviewed_at: 2026-08-21T09:20:00Z
base_revision: 768ec1f3013f6d0eccd7c8b590ba36b54b12d23e
decision: approved
scope:
  - ta-16-migration-force-rls-never-enables characterization (EnableRLS(force=True) SQL emission vs PostgreSQL ENABLE/FORCE catalog flags)
  - Isolated strict xfail test_force_rls_without_enable_rls_grants_no_isolation_defect
  - RLS fixture isolation (_apply_schema vs _apply_rls_schema two-op workaround)
  - AGENTS.md §3 migration-safety / RLS: Wave 0 must not treat force=True as safe; production ENABLE+FORCE fix is W1-C residual
  - No secrets/DSNs/row data in Ferrum default output from this owned-path change
---

# Named Authority Verdict

## Authority

`SecurityEngineer`

This record reviews W0-B consumer-parity run `20260821T075801Z` after the executor reproduced a live-PostgreSQL RLS defect and correctly raised the security gate. Original task-contract `security_review: false` (inventory-only) is superseded by the finding; the workstream yaml already records `rls_admin_gucs: true` and `security_review: required`.

This record does not implement code, does not edit Ferrum production SQL/migration paths, does not start Wave 1, and does not persist itself under `reviews/`. It does not substitute for ChiefArchitect, CodeReviewer, ProductManager, or independent verification.

Mapped quality gate: **Pass with follow-ups** of the *inventory and xfail characterization* → `approved`. Approving this run means the hole is correctly stated and Wave 0 must not pretend `EnableRLS(force=True)` is safe. It does **not** clear the production defect.

## Claims reviewed

1. **Emission hole (ta-16).** `python/ferrum/migrations/orchestrator.py` `enable_rls` with `EnableRLS(table, force=True)` emits only `ALTER TABLE ... FORCE ROW LEVEL SECURITY` and never the paired `ALTER TABLE ... ENABLE ROW LEVEL SECURITY`.
2. **PostgreSQL semantics.** `FORCE` and `ENABLE` are independent catalog flags (`relforcerowsecurity` vs `relrowsecurity`). If `relrowsecurity` stays false, policies are a silent no-op for every role that is not already `BYPASSRLS`/superuser — not merely the narrower table-owner bypass that `FORCE` is meant to close.
3. **Consumer citation.** Ticket Analyzer `migrations/0018-force-rls.sql` applies `FORCE` as defense-in-depth on tables that already received `ENABLE` (e.g. `migrations/0002-worker-init.sql`). That consumer sequence is ENABLE-then-FORCE. The Ferrum hole is collapsing `force=True` into FORCE-only, which the public `EnableRLS` name, `docs/api-reference.md` (`ENABLE [FORCE]`), and existing `tests/python/integration/test_ticket_analyzer_compat.py` all invite.
4. **Contract test.** `test_force_rls_without_enable_rls_grants_no_isolation_defect` is `pytest.mark.xfail(strict=True)`, applies **only** `EnableRLS(..., force=True)` plus a `team_isolation` policy, and asserts the correct/fixed behavior (zero rows with no `app.team_id` GUC). That currently fails as a both-team leak. An accidental production fix without removing the marker would fail as unexpected-pass.
5. **Owned-path scope.** This workstream owns `tests/consumer_contracts/` only. The two-op workaround in `_apply_rls_schema` is a test fixture, not a production fix. W0-B must not edit `orchestrator.py`.
6. **§3 default-output hygiene.** Inventory/tests must not leak connection strings, passwords, bound values, or row data in Ferrum default hooks/errors/logs.

Out of this gate: ta-15 `_DEFAULT_VALUE_ALLOWLIST` (architecture/product, not an RLS silent-bypass). Out of this gate: implementing ENABLE+FORCE in production (W1-C, leased migration workstream). This record grants only the SecurityEngineer gate for run `20260821T075801Z`.

## Evidence

Inspected current source at `base_revision` `768ec1f3013f6d0eccd7c8b590ba36b54b12d23e` plus the W0-B owned-path additions. Executor log `logs/w0-b-consumer-parity/20260821T075801Z.md` was read only as a claim list; emission, catalog semantics, consumer SQL, and test structure below are from source. No production files were edited. No tests were re-executed in this review (review-only). No `reviews/` file was written by this agent.

### Emission — FORCE-only when `force=True`

```637:641:python/ferrum/migrations/orchestrator.py
 if kind == "enable_rls":
 table = op["table"]
 if op.get("force"):
 return f"ALTER TABLE {_quote_ident(table, dialect)} FORCE ROW LEVEL SECURITY"
 return f"ALTER TABLE {_quote_ident(table, dialect)} ENABLE ROW LEVEL SECURITY"
```

`if op.get("force")` returns early. There is no second statement, no comma-list `ENABLE ROW LEVEL SECURITY, FORCE ROW LEVEL SECURITY`, and no fallback ENABLE. Table identifiers go through `_quote_ident` (allowlisted/quoted); this branch does not interpolate user values into SQL. The defect is missing ENABLE, not identifier injection.

`EnableRLS` (`python/ferrum/migrations/operations.py:464-488`) is classified `safe`. The class docstring says `ALTER TABLE t ENABLE ROW LEVEL SECURITY` and that `force=True` “emits `FORCE ROW LEVEL SECURITY` so that table owners are also subject to the policies.” `to_op_dict` carries `"force": self.force`. The public docs table (`docs/api-reference.md:680`) documents `ALTER TABLE "t" ENABLE [FORCE] ROW LEVEL SECURITY`. Current emission does not match that documented SQL: PostgreSQL has no `ENABLE [FORCE]` clause; ENABLE and FORCE are separate `ALTER TABLE` actions (`relrowsecurity` vs `relforcerowsecurity`).

Unit test `tests/python/unit/test_new_operations.py:132-136` (`test_sql_emission_force`) asserts `"FORCE ROW LEVEL SECURITY" in sql` and does **not** assert ENABLE is also present. That unit test currently ratifies the hole.

### PostgreSQL semantics — policies are a no-op until ENABLE

PostgreSQL applies RLS policies only when `pg_class.relrowsecurity` is true. `FORCE ROW LEVEL SECURITY` sets `relforcerowsecurity`, which additionally subjects the table owner once RLS is enabled. Superuser and `BYPASSRLS` bypass both flags.

Consequence of FORCE-only: `relrowsecurity=false`, policies exist, every ordinary (including table-owner) session sees all rows. That is a complete silent isolation failure, strictly worse than the owner-bypass gap 0018 documents.

Existing Ferrum compat coverage does not catch this. `tests/python/integration/test_ticket_analyzer_compat.py:159` applies `ops.EnableRLS(issue_table, force=True)` with no prior plain `EnableRLS`. `test_tenant_transaction_binds_team_guc` (`:279-286`) then filters `Issue.objects.filter(team_id=team_a.id)` because “CI uses a PostgreSQL superuser, which bypasses FORCE RLS.” That test proves GUC bind, not policy evaluation. A superuser/`BYPASSRLS` role, or FORCE-only with RLS never enabled, both yield “query returns the filtered row.” W0-B’s isolated xfail is the first test that actually demands policy evaluation.

### Consumer citation — ENABLE already present; FORCE is additive

`/Users/guyshaked/Desktop/dev/repos/ticket-analyzer-agent/migrations/0018-force-rls.sql:3-20` (and `:20-26` DDL): FORCE on `tickets`/`alerts`/`alert_rules`/`chat_conversations`/`chat_messages`/`issues`/`llm_usage`. Comment states policies are not applied to the table owner unless FORCE is set.

Prior ENABLE for those tables exists, e.g. `migrations/0002-worker-init.sql:85,93,101,109,117` (`ALTER TABLE tickets ENABLE ROW LEVEL SECURITY` and the same for the other 0002 tenant tables); `llm_usage` ENABLE is `migrations/0015-llm-usage.sql:35`; `issues` ENABLE is `migrations/0011-issues.sql:41`. Ticket Analyzer does **not** currently ship FORCE without ENABLE.

Manifest `ta-16` (`tests/consumer_contracts/manifest.py:608-664`) states that correctly: 0018 is FORCE on top of earlier ENABLE; a Ferrum-migrated defense-in-depth step is `ops.EnableRLS(table, force=True)`; the Ferrum defect is FORCE-only leaving `relrowsecurity=false`; the xfail reproduces a single `force=True` op plus policy leaking both teams with no GUC. That is an accurate split: consumer SQL sequence vs Ferrum single-op API.

A faithful two-migration Ferrum port (`EnableRLS(t)` then `EnableRLS(t, force=True)`) would work with today’s emitter. The security hole is the public one-op `force=True` path, which Ferrum’s own docs and `test_ticket_analyzer_compat.py` already use.

### Contract tests — xfail present, isolated, asserts the fixed behavior

`tests/consumer_contracts/test_ticket_analyzer_contracts.py:269-343`:

- `@pytest.mark.xfail(..., strict=True)` with a reason that names the FORCE-only emission and silent policy no-op.
- Builds its own schema; deliberately omits plain `EnableRLS`.
- Applies `EnableRLS(event_table, force=True)` + `CreatePolicy("team_isolation", ...)`.
- Inserts one row per team through the raw connection (no GUC).
- Asserts `Event.objects.all(pg_conn) == []` — the correct result if RLS were enabled (unset `current_setting('app.team_id', true)` matches no `team_id`).
- Independent teardown (`DropPolicy` + `DisableRLS` + `_drop_schema`).

`_apply_schema` (`:83-123`) has **no** RLS ops; used by non-RLS contract tests so they can write through `pg_conn` without a tenant GUC. `_apply_rls_schema` (`:158-197`) uses the two-op workaround (`EnableRLS(t)` then `EnableRLS(t, force=True)`) plus `team_isolation` and `platform_admin_bypass`, and is only consumed by `rls_contract_models` / `test_platform_admin_bypass_sees_all_teams`. That split is required: once FORCE+ENABLE actually fire, unscoped inserts through the table-owner role would hit `InsufficientPrivilegeError`. Isolating the force-only xfail from that workaround is correct; sharing the workaround would hide the defect.

Comment/xfail-reason strings in this test file repeatedly say `ta-15-migration-force-rls-never-enables`. Manifest id is `ta-16-migration-force-rls-never-enables`; `ta-15` is the DEFAULT-literal allowlist defect. The hole itself is not mis-stated. Label drift is a non-blocking follow-up inside the owned path.

Executor-claimed live result (not re-run here; independent verification owns a fresh live replay): against a non-superuser, non-`bypassrls` role, `pytest tests/consumer_contracts/ -v -m integration` → 9 passed, 1 xfailed (`test_force_rls_without_enable_rls_grants_no_isolation_defect`). That role distinction matters: a superuser would make both the xfail and the two-op admin-bypass test meaningless.

### §3 default-output / secrets

Owned-path tests do not print DSNs, passwords, bound parameter values, or row payloads. `conftest.py` reads `FERRUM_TEST_DSN` and skips if unset; it does not log the value. `apply(..., dry_run=False)` in these fixtures is test-only against disposable tables.

The executor log records a throwaway DSN including a password (`postgresql://w0b_app:w0b_app_pw@localhost:55432/w0b_scratch`) and states the container was removed. That is not Ferrum default hook/exception/migration output. It is process hygiene for append-only logs, not a §3 product-output leak, and is not `changes_required` for this gate.

No Ferrum production SQL/migration file is in this run’s `changed_paths`.

## Findings

### Critical / High (must-fix in this W0-B owned path)

None. The inventory and xfail correctly characterize the hole. The xfail is present, `strict=True`, isolated from the two-op workaround, and asserts the safe behavior. Default Ferrum output from this change does not leak secrets/DSNs/row data.

### Medium (production residual — W1-C; do not treat as W0-B must-fix)

1. **`EnableRLS(force=True)` never ENABLES.** `orchestrator.py:637-641`. W1-C must emit ENABLE and FORCE together when `force=True` (one `ALTER TABLE` with two actions, or two statements on the same connection). FORCE must not substitute for ENABLE. Do not document the current FORCE-only SQL as supported.
2. **Public docs and the `EnableRLS` name promise ENABLE.** `docs/api-reference.md:680`; `operations.py:464-472`. W1-C must make emission match. PostgreSQL has no `ENABLE [FORCE]` syntax; use `ENABLE ROW LEVEL SECURITY` plus `FORCE ROW LEVEL SECURITY`.
3. **Existing compat fixture uses the defective one-op path and does not prove isolation.** `test_ticket_analyzer_compat.py:159,279-286`. After the W1-C fix, that suite must assert policy evaluation on a non-superuser, non-`bypassrls` role (unscoped `all()` with no GUC → zero rows; other-team GUC → zero rows), not `filter(team_id=...)` under a superuser.
4. **Unit test currently ratifies FORCE-only.** `test_new_operations.py:132-136`. W1-C must require ENABLE in the `force=True` SQL (and keep FORCE).

### Low (owned-path nits; not `changes_required`)

5. **ta-15 vs ta-16 label drift** in `test_ticket_analyzer_contracts.py` comments and the xfail `reason` (`:166,174,272,289`). Manifest id is `ta-16`. Fix in a later owned-path edit or when the xfail is removed after W1-C.
6. **Task-contract frontmatter still says `security_review: false` / `rls_admin_gucs: false`.** Workstream yaml already raised the gate. Coordinator should align the task contract; not a characterization defect.

## Decision

`approved`

**Confirm the diagnosis.** `EnableRLS(table, force=True)` emits only `FORCE ROW LEVEL SECURITY`. With `relrowsecurity` left false, RLS policies are a silent no-op — a complete isolation failure, not the owner-bypass gap Ticket Analyzer 0018 closes. Consumer 0018 is FORCE-on-top-of-ENABLE; the Ferrum hole is the single-op `force=True` API that never ENABLES.

**Confirm the xfail is the right Wave 0 artifact.** `test_force_rls_without_enable_rls_grants_no_isolation_defect` is isolated, `strict=True`, and asserts zero rows with no tenant GUC. `_apply_rls_schema` correctly keeps the two-op workaround off the non-RLS fixtures. Wave 0 must not pretend `force=True` is safe.

**This approval does not clear the production defect.** Do not implement ENABLE+FORCE in this workstream. Record the fix as residual for a later leased migration workstream (**W1-C**), which already owns `orchestrator.apply()` transactionality, destructive confirm, and migration SQL emission. W1-C must also retarget `test_sql_emission_force` and `test_ticket_analyzer_compat.py` so they cannot false-pass on superuser or FORCE-only SQL.

**§3.** No new default-hook, exception, or migration-output leak from the owned-path inventory. SQL identifiers in this branch remain quoted. `CreatePolicy` `using`/`check_expr` remain developer-supplied migration SQL (pre-existing; not introduced here).

Coordinator: persist this record verbatim at
`.agent-work/production-readiness/reviews/w0-b-consumer-parity/20260821T075801Z-security-engineer.md`.

### Missing tests (assign to W1-C; do not claim covered by W0-B)

- No production-path (non-xfail) test that `EnableRLS(t, force=True)` sets both `relrowsecurity` and `relforcerowsecurity` (query `pg_class` or equivalent).
- No security test that a non-superuser, non-`bypassrls` table-owner session with only `force=True` historically leaked and, after the fix, returns zero rows without a GUC.
- No test that `test_ticket_analyzer_compat.py` isolation holds without `filter(team_id=...)` under a non-bypass role.
- No test that `DisableRLS` after FORCE+ENABLE clears both flags (or that `NO FORCE` is available if W1-C adds it). These are implementation tests, not W0-B inventory gaps.

### Recommendations (non-blocking)

- W1-C preferred emission: `ALTER TABLE "t" ENABLE ROW LEVEL SECURITY, FORCE ROW LEVEL SECURITY` (single command, two actions) so apply cannot ENABLE without FORCE if the process dies between statements. If two `execute` calls are used, document the partial-apply failure mode under ADR-004’s still-reopened non-transactional apply.
- Keep `DisableRLS` destructive and confirm-gated (already). Do not add a silent `force=True` autodiff collapse that drops a prior ENABLE.
- Live RLS proofs must use a role with `rolsuper=f` and `rolbypassrls=f`. Superuser CI cannot validate FORCE or ENABLE.
- Do not persist live passwords in append-only executor logs; use redacted DSNs (`postgresql://w0b_app@localhost:55432/w0b_scratch`).
- When W1-C lands, remove the W0-B `xfail` in the same change (strict xfail will fail as unexpected-pass until the marker is dropped).

This record grants only the SecurityEngineer gate. It does not substitute for another authority or independent verification.
