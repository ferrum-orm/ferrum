---
task_id: w1-f-tenancy-shards
run_id: 20260829T085649Z
authority: ChiefArchitect
reviewer: chief-architect-agent
reviewed_at: 2026-08-29T12:10:00Z
base_revision: 71e04328688bf9142751aa8a2c1c59dc1a69b410
decision: approved
scope:
  - Architecture of platform_admin_transaction (session.py)
  - Architecture of schema_transaction (session.py)
  - Architecture of ConnectionRegistry/ShardRouter (routing.py)
  - AGENTS.md §5a "Schema tenancy and sharding boundaries" contract conformance
---

# Named Authority Verdict — ChiefArchitect

## Authority

ChiefArchitect — ratifies ADRs, root architecture contracts, new components, and
new failure modes (PROTOCOL §"Authority and hard stops").

## Claims reviewed

The architecture of three new public components against the **binding** §5a
"Schema tenancy and sharding boundaries" ratified contract:

- **A1**: `platform_admin_transaction()` sets ONLY allowlisted admin GUCs and
  needs no fake tenant id (§5a item 1).
- **A2**: `schema_transaction(schema, ...)` validates the schema identifier
  against a strict allowlist (never string-interpolated from untrusted input)
  and sets a transaction-local `search_path` on one pinned transaction — this is
  validated schema selection, not implicit routing (§5a item 2).
- **A3**: `ConnectionRegistry`/`ShardRouter` own independently configured
  **PostgreSQL** pools (not a multi-database or dialect-switching Session); the
  router resolves a **trusted** shard key chosen by caller/router code and
  returns an explicit `Connection`/`Transaction`; QuerySet stays shard-unaware
  and connection-explicit (§5a item 3).
- **A4**: No `platform_scoped` model flag; no implicit connection selection
  from model metadata, tenant id, or schema name.
- **A5**: No implicit multi-DB behavior.
- **A6**: mysql/sqlite/mssql extras rejected at registry level (ProductManager
  resolution A caveat: "not a license for W1-F to launder dialect switching
  through the registry").

## Evidence

Inspected directly (no reliance on executor or generic-verifier summaries):

### `git diff HEAD -- python/ferrum/session.py`
- `ALLOWED_ADMIN_GUC_NAMES = frozenset({"app.platform_admin", "ferrum.admin"})`
  — strict subset of `ALLOWED_GUC_NAMES`; tenant-id GUCs (`app.team_id`, etc.)
  are structurally excluded. Comment documents the §5a rationale.
- `ALLOWED_SCHEMA_NAMES = frozenset({"public"})` — strict default; callers
  extend at startup or per-call via `allowed_schemas=`.
- `_SCHEMA_IDENT_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,62}$")` — strict
  PostgreSQL identifier pattern; duplicated locally so session.py does not
  import a private symbol from connection.py (clean module boundary).
- `_validate_admin_guc` / `_validate_schema_name`: regex THEN allowlist, both
  fail-closed with structured `FerrumCompileError` categories
  (`guc_name_not_allowed` / `invalid_identifier` / `schema_not_allowed`).
- `platform_admin_transaction`: calls `_validate_admin_guc(admin_guc)` BEFORE
  `conn.transaction(...)`; inside the tx sets ONLY `set_config(tx, admin_guc,
  "true")`. No tenant-id GUC is set. ✓ A1
- `schema_transaction`: calls `_validate_schema_name(schema, allow)` BEFORE
  `conn.transaction(...)`; inside the tx calls `set_config(tx, "search_path",
  schema)`. The existing `set_config` (session.py:94-113) hardcodes
  `set_config('{name}', $1, true)` — third arg `true` = transaction-local, and
  the schema value is the bound parameter `$1`, never interpolated. The GUC
  name `"search_path"` is allowlisted (added to `ALLOWED_GUC_NAMES`). ✓ A2

### `python/ferrum/routing.py` (new, 365 lines, Read in full)
- `_ensure_postgres_dsn` (lines 55-68): `urlparse(dsn).scheme.lower()` must be
  in `{"postgresql", "postgres"}`; else `FerrumConfigError`. Called for every
  config in `__init__` — structural, not conventional. ✓ A6
- `PoolConfig` (lines 71-95): frozen dataclass mirroring `Connection` pool
  kwargs; each shard is independently sized/tuned. ✓ A3
- `ConnectionRegistry.__init__` (lines 122-149): rejects `parallelism < 1`,
  empty configs, empty shard names; calls `_ensure_postgres_dsn` per config.
  No dialect switching, no model-metadata inspection.
- `start()` (lines 179-226): bounded `asyncio.Semaphore`; on `BaseException`
  closes already-opened pools (`asyncio.gather(..., return_exceptions=True)`)
  and re-raises — no partial registry left behind. ✓ A3
- `get(name)` (lines 156-177): explicit, named; raises `FerrumConfigError` for
  unknown / not-started / closed. No implicit selection.
- `health_check()` (lines 228-250): concurrent probe; failing pool reported
  `False` without aborting the rest. `Exception` (not `BaseException`) is
  caught — `CancelledError` propagates correctly.
- `close()` (lines 252-271): idempotent (`_closed` guard); bounded parallel;
  `return_exceptions=True` so one failing close does not mask others.
- `stats()` (lines 273-279): per-shard `PoolStats | None`.
- `ShardRouter` (lines 282-365): wraps `registry` + caller-supplied
  `resolver: Callable[[ShardKeyT], str]`. The resolver is the SINGLE place
  routing policy lives. `connection_for` → `self._resolver(shard_key)` →
  `registry.get(name)` → explicit `Connection`. `transaction_for` opens
  `conn.transaction(...)` on the resolved shard and yields the `Transaction`.
  The router NEVER inspects model metadata, tenant ids, or schema names. ✓ A3
- grep for `platform_scoped` across `python/ferrum/`: exactly 1 match, in
  routing.py:18 docstring documenting its absence. No code defines it. ✓ A4
- The registry is PostgreSQL-only; no `Session`-level dialect switching; no
  multi-DB behavior. ✓ A5

### `git diff HEAD -- python/ferrum/__init__.py`
- 5 new entries in `__all__` (`ConnectionRegistry`, `PoolConfig`,
  `ShardRouter`, `platform_admin_transaction`, `schema_transaction`) and
  matching imports. The `from ferrum.session import` reorder is isort
  consolidation — formatting only, no API change. Exports are additive.

### Architecture composition check (orthogonality)
`ShardRouter.transaction_for` yields a `Transaction`; `tenant_transaction` /
`platform_admin_transaction` / `schema_transaction` take a `Connection`. The
router and session helpers are **orthogonal composable primitives**: a caller
who wants RLS + shard routing uses `router.connection_for(key)` to get the
explicit `Connection`, then nests the relevant session helper. There is no
magic auto-composition, no implicit nesting, and no hidden GUC binding across
the router boundary. This matches §5a's "QuerySet stays shard-unaware and
connection-explicit; it receives whatever `Connection`/`Transaction` the router
hands off." The architecture is clean.

## Findings

| # | Severity | Finding | Required correction |
|---|---|---|---|
| 1 | Info | `ConnectionRegistry.start()` populates `self._conns` in pool-open *completion* order (via the shared `opened` list), not `self._configs` order. The inline comment "Preserve insertion order for deterministic stats()/names()" is slightly inaccurate: `names` uses `self._configs` (deterministic), but `stats()` iterates `self._conns` (completion order). `stats()` is a snapshot dict whose ordering is not contractually guaranteed, so this is cosmetic. | Optional: iterate `self._configs` in `stats()` for deterministic ordering, or amend the comment. Not blocking. |
| 2 | Info | `ConnectionRegistry.close()` docstring states exceptions are "aggregated", but `asyncio.gather(..., return_exceptions=True)` returns the exceptions to a list that is then discarded — they are swallowed, not surfaced. Best-effort close is the correct behavior; the docstring overstates the contract. | Optional: amend docstring to "best-effort; individual close exceptions are suppressed so one failing pool does not mask others." Not blocking. |
| 3 | Info | `ConnectionRegistry` has no `__aenter__`/`__aexit__`; callers use try/finally with `start()`/`close()`. An `async with` would be more ergonomic but §5a does not mandate it. | Optional future enhancement; not an architectural violation. |
| 4 | Info | Session helpers (`platform_admin_transaction`, `schema_transaction`) expose only `isolation`/`readonly`, while `ShardRouter.transaction_for` also exposes `deferrable`/`deadline`. A caller needing `deferrable` + admin GUC must use `connection_for` + manual nesting. This is acceptable for v0.1 (session helpers are GUC-binding wrappers, not full transaction-config proxies) but is a minor API surface inconsistency. | Optional: consider forwarding `deferrable`/`deadline` in a future revision. Not blocking. |

None of the findings are architectural violations. All are info-level
ergonomics/documentation observations suitable for a future revision or
CodeReviewer follow-up. They do not block this gate.

## Decision

**approved**

The architecture of `platform_admin_transaction`, `schema_transaction`, and
`ConnectionRegistry`/`ShardRouter` faithfully implements the ratified §5a
"Schema tenancy and sharding boundaries" contract:

- `platform_admin_transaction` sets only allowlisted admin GUCs, validates
  before opening the tx, and needs no fake tenant id (A1 ✓).
- `schema_transaction` validates the schema identifier against a strict regex
  AND allowlist before opening the tx, and sets a transaction-local
  `search_path` via `set_config(..., true)` with the value as a bound
  parameter — validated schema selection on one pinned transaction, not
  implicit routing (A2 ✓).
- `ConnectionRegistry`/`ShardRouter` own independently configured
  PostgreSQL-only pools (non-postgres DSNs rejected structurally at
  construction); the router resolves a trusted caller-supplied shard key to an
  explicit `Connection`/`Transaction`; QuerySet stays shard-unaware and
  connection-explicit; no `platform_scoped` flag; no implicit connection
  selection from model metadata, tenant id, or schema name; no implicit
  multi-DB behavior; mysql/sqlite/mssql rejected at the registry level (A3,
  A4, A5, A6 ✓).

The router and session helpers are orthogonal composable primitives with no
magic nesting — the correct architecture for §5a. Findings 1-4 are info-level
and do not block.

This record grants only the ChiefArchitect gate. It does not substitute for
the SecurityEngineer gate (required for rls_admin_gucs + schema_selection) or
the CodeReviewer gate, both of which remain required per the task contract.
