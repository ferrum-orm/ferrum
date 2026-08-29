---
task_id: w1-f-tenancy-shards
run_id: 20260829T085649Z
authority: SecurityEngineer
reviewer: security-engineer-agent
reviewed_at: 2026-08-29T11:30:00Z
base_revision: 71e04328688bf9142751aa8a2c1c59dc1a69b410
decision: approved
scope:
  - python/ferrum/session.py
  - python/ferrum/routing.py
  - python/ferrum/__init__.py
  - tests/python/unit/test_session.py
  - tests/python/unit/test_routing.py
  - tests/python/integration/test_session_rls_shards.py
---

# Named Authority Verdict

## Authority

SecurityEngineer

## Claims reviewed

Security-gated claims under AGENTS.md §3 (Security rules), §5a (Schema tenancy
and sharding boundaries — BINDING), and the task contract's
`security_surfaces: {rls_admin_gucs: true, schema_selection: true}`:

- **S1** — Admin GUCs are STRICTLY allowlisted; no arbitrary GUC setting; no
  fake tenant id on the admin path.
- **S2** — Schema identifiers validated against a strict allowlist AND an
  identifier regex; NEVER interpolated from untrusted input (bound parameter).
- **S3** — `search_path` is transaction-local; resets on commit/rollback/pool
  reuse/cancellation.
- **S4** — Shard keys are trusted caller/router values; no implicit routing
  from untrusted input (model metadata, tenant id, schema name).
- **S5** — Cancellation does NOT leak GUC or `search_path` onto a pooled
  connection.
- **S6** — No `platform_scoped` model flag.
- **S7** — No implicit QuerySet connection selection.
- **S8** — mysql/sqlite/mssql rejected at registry construction
  (PostgreSQL-only).

## Evidence

Inspected paths (independent of executor/verifier summaries):

- `git diff HEAD -- python/ferrum/session.py python/ferrum/__init__.py`
- `Read python/ferrum/routing.py` (new, 365 lines)
- `Read python/ferrum/session.py:75-149` (existing `set_config` / `current_setting`)
- `grep platform_scoped python/ferrum/` → 1 match, in `routing.py:18`
  docstring ("There is no `platform_scoped` model flag") — the contract
  assertion, NOT a flag definition. ✓
- `grep` for security-critical integration test names in
  `tests/python/integration/test_session_rls_shards.py` → all 6 present.

Commands run with meaningful output:

```
uv run pytest tests/python/unit/test_session.py tests/python/unit/test_routing.py -x -q
→ 65 passed in 0.23s
```

### S1 — Admin GUCs strictly allowlisted

`python/ferrum/session.py`:
- `ALLOWED_ADMIN_GUC_NAMES = frozenset({"app.platform_admin", "ferrum.admin"})`
  — strict subset of `ALLOWED_GUC_NAMES`; tenant-id GUCs (`app.team_id`)
  intentionally excluded.
- `_validate_admin_guc(name)` rejects anything not in the set with structured
  `FerrumCompileError(category="guc_name_not_allowed")` (FERR-C102).
- `platform_admin_transaction(conn, *, admin_guc="app.platform_admin", ...)`:
  calls `_validate_admin_guc(admin_guc)` BEFORE `conn.transaction(...)` — fails
  before opening the tx (no partial state). Inside the tx it calls
  `set_config(tx, admin_guc, "true")` — sets ONLY the admin flag; no
  `app.team_id` is set. No fake tenant id. ✓
- Unit test `test_tenant_guc_rejected_as_admin` confirms `app.team_id` is
  rejected as an admin GUC.

### S2 — Schema identifiers validated (regex AND allowlist; bound parameter)

`python/ferrum/session.py`:
- `_SCHEMA_IDENT_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,62}$")` — strict
  PostgreSQL identifier pattern; rejects `;`, spaces, quotes, dots, `--`, and
  overlong (>63 char) identifiers.
- `ALLOWED_SCHEMA_NAMES = frozenset({"public"})` — strict default allowlist;
  callers extend via `allowed_schemas=` per-call or by mutating the module set
  at startup.
- `_validate_schema_name(schema, allowed)` checks regex FIRST, then allowlist.
  Both fail-closed with structured `FerrumCompileError` categories
  (`invalid_identifier`, `schema_not_allowed`).
- `schema_transaction(conn, schema, ...)` calls `_validate_schema_name` BEFORE
  `conn.transaction(...)` — fails before opening the tx.
- The schema *value* reaches SQL via `set_config(tx, "search_path", schema)`.
  In `set_config` (session.py:94-113), the value is bound as `$1`:
  `await driver.execute(f"SELECT set_config('{name}', $1, true)", value)`.
  The schema identifier is a **bound parameter** — NEVER interpolated into the
  SQL string. The GUC *name* (`"search_path"`) is interpolated via f-string
  but is allowlist-validated by `_validate_guc_name` inside `set_config`, and
  is a literal constant at the `schema_transaction` call site — not user
  input. ✓
- Unit tests: `test_injection_attempt_rejected_by_regex`,
  `test_quoted_identifier_rejected_by_regex`,
  `test_dotted_schema_rejected_by_regex`, `test_overlong_identifier_rejected`,
  `test_valid_identifier_not_in_allowlist_rejected`.

Adversarial check: `schema_transaction(conn, "public; DROP TABLE x")` → regex
fails → `FerrumCompileError(category="invalid_identifier")` BEFORE the tx
opens. Even if the regex were bypassed, the allowlist would reject. Even if
both were bypassed, the value is bound as `$1` so it cannot inject SQL.
Defense in depth (three layers). ✓

### S3 — `search_path` is transaction-local

`set_config("search_path", schema, true)` — third arg `true` =
transaction-local. PostgreSQL resets transaction-local GUCs on commit/rollback,
and asyncpg/Ferrum pool reuse returns connections in a clean state. The
`search_path` cannot persist onto a pooled connection after the transaction
ends. Integration tests confirm reset after commit
(`test_schema_transaction_sets_search_path`) and after rollback
(`test_schema_transaction_search_path_resets_on_rollback`). ✓

### S4 — Shard keys are trusted caller values

`python/ferrum/routing.py`:
- `ShardRouter.__init__(registry, resolver: Callable[[ShardKeyT], str])` —
  the resolver is caller-supplied; it is the SINGLE place routing policy
  lives.
- `connection_for(shard_key)` calls `self._resolver(shard_key)` then
  `registry.get(name)`. The router NEVER inspects model metadata, tenant ids,
  or schema names to pick a connection.
- No `platform_scoped` model flag exists (grep confirms only the docstring
  assertion in `routing.py:18`).
- QuerySet stays shard-unaware: `transaction_for` yields a `Transaction` the
  caller passes explicitly to QuerySet terminals; no implicit connection
  selection. ✓

### S5 — Cancellation does NOT leak GUC or search_path

Integration tests against live PostgreSQL (per verifier record; test names
confirmed present):
- `test_cancellation_does_not_leak_admin_guc` — task cancelled mid `pg_sleep(2)`
  inside `platform_admin_transaction`; pool read after cancel asserts
  `app.platform_admin` is gone.
- `test_cancellation_does_not_leak_search_path` — same pattern for
  `schema_transaction`; `search_path` is gone after cancel.

Both rely on transaction-local `set_config(..., true)`; cancellation triggers
rollback which resets the GUC. ✓

### S6 — No `platform_scoped` model flag

`grep platform_scoped python/ferrum/` → 1 match: `routing.py:18` docstring
("There is no `platform_scoped` model flag"). No flag definition, no model
attribute, no metadata key. ✓

### S7 — No implicit QuerySet connection selection

QuerySet terminals still require an explicit `conn` argument (unchanged by
this diff — `session.py` and `routing.py` do not touch `queryset.py`;
verifier confirmed 0 W1-F symbols in `queryset.py`/`connection.py`/`runtime.py`).
`ShardRouter.transaction_for` yields a `Transaction` the caller passes
explicitly. No model-metadata-driven routing in `routing.py`. ✓

### S8 — mysql/sqlite/mssql rejected at registry construction

`python/ferrum/routing.py:55-68`:
- `_ensure_postgres_dsn(dsn)` parses the scheme via `urlparse(dsn).scheme.lower()`
  and rejects anything not in `_POSTGRES_SCHEMES = {"postgresql", "postgres"}`
  with `FerrumConfigError` (FERR-C001).
- `ConnectionRegistry.__init__` calls `_ensure_postgres_dsn(cfg.dsn)` for
  EVERY config in the constructor loop — structural enforcement, not
  convention. A non-postgres DSN cannot reach `start()`.
- Unit tests: `test_mysql_dsn_rejected`, `test_sqlite_dsn_rejected`,
  `test_registry_rejects_non_postgres_dsn`. ✓

## Findings

| # | Severity | Finding | Required correction |
|---|---|---|---|
| 1 | Info | `set_config` interpolates the GUC *name* via f-string (`f"SELECT set_config('{name}', $1, true)"`). The name is allowlist-validated (`_validate_guc_name`) and is a literal constant at the `schema_transaction` call site, so this is safe. A future hardening could bind the name as a parameter too, but this is the existing shipped pattern and is NOT required by §5a (which governs the schema *value*, bound as `$1`). | None for W1-F. Optional future hardening out of scope. |
| 2 | Info | Default `ALLOWED_SCHEMA_NAMES={"public"}` is intentionally strict; app callers must register tenant schemas at startup or pass `allowed_schemas=` per call. This is the correct fail-closed design but should be documented when the docs lease is granted. | None for W1-F code. Docs note when README/CHANGELOG lease is granted. |
| 3 | Info | `_ensure_postgres_dsn` validates the DSN scheme only (not the full DSN structure). This is sufficient for the "PostgreSQL-only" contract — a non-postgres scheme is structurally rejected at construction. Full DSN validation is `Connection.open()`'s responsibility (out of scope for the registry). | None. |

No blockers, no `changes_required` findings. All eight security claims (S1–S8)
have deterministic evidence from independent source inspection and passing
unit tests. The integration cancellation/leak tests (per verifier record)
confirm the transaction-local reset property against live PostgreSQL.

## Decision

**approved**

This record grants only the SecurityEngineer gate for W1-F (rls_admin_gucs +
schema_selection surfaces). It does not substitute for the ChiefArchitect or
CodeReviewer gates, which are also required by the task contract and must
record their own `decision: approved` artifacts. Independent technical
verification (verifier record `20260829T085649Z.md`) is clean; no W1-F code
changes are required from this review.
