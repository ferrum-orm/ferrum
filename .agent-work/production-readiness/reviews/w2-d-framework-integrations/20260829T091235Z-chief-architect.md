---
task_id: w2-d-framework-integrations
run_id: 20260829T091235Z
authority: ChiefArchitect
reviewer: ChiefArchitect
reviewed_at: 2026-08-29T10:45:00Z
base_revision: b5e7ed3beaab60b7ded6ff6b1f8b77293ad376bb
decision: approved
scope:
  - FastAPI integration architecture
  - One-pool-per-process lifespan
  - Transaction-scoped request dependencies
  - Ferrum→HTTP error translation architecture
  - Optional fastapi-users adapter design
  - Core Ferrum import boundary (never imports FastAPI)
---

# Named Authority Verdict

## Authority

ChiefArchitect

## Claims reviewed

- **C1**: Hardened `ferrum_lifespan` opens exactly one pool per process and
  inherits the W1-E `_EventLifecycleGuard` event-based drain; all W1-E pool
  knobs thread through.
- **C2**: `get_ferrum_transaction` yields a request-scoped `Transaction` via
  `Depends`; commits on clean exit, rolls back on exception; matches
  ratified §5a object-scoped retry-disable.
- **C3**: Ferrum→HTTP error translation architecture maps exception classes
  to status codes and emits only sanctioned safe fields; never leaks
  DSNs, bound values, or row data.
- **C4**: Optional `fastapi-users` adapter (`FerrumUserDatabase`)
  soft-imports the protocol, raises clear `ImportError` when missing,
  adds no hard dependency; caller controls transaction boundaries.
- **C5**: Core Ferrum never imports `fastapi`/`starlette`/`fastapi_users`;
  `ferrum.contrib.__init__` does not import FastAPI; import boundary
  enforced by `.importlinter` contracts in CI.

## Evidence

### Diff scope (independent `git diff HEAD --stat`)

```
 python/ferrum/contrib/__init__.py         |   9 +-
 python/ferrum/contrib/fastapi.py          | 505 +++++++++++++++++++++++++++-
 tests/python/unit/test_contrib_fastapi.py | 525 ++++++++++++++++++++++++++++--
 3 files changed, 1001 insertions(+), 38 deletions(-)
```

`tests/python/integration/test_fastapi_integration.py` is a new untracked
file (451 lines). No shared paths (`pyproject.toml`,
`python/ferrum/__init__.py`, `README.md`, `CHANGELOG.md`) touched — verified
with `git diff HEAD --name-only -- pyproject.toml python/ferrum/__init__.py`
(empty).

### C1 — One-pool-per-process lifespan (source inspection)

`ferrum_lifespan` (`python/ferrum/contrib/fastapi.py`, diff) is an
`@contextlib.asynccontextmanager` that calls `connect(...)` once and yields
the resulting `Connection`. In Ferrum's W1-E design, `Connection` IS the
pool — `ferrum.connect()` (`python/ferrum/connection.py:1043`) returns a
`Connection` wrapping the asyncpg pool with `_EventLifecycleGuard`. The
lifespan yields exactly once; there is no per-request `connect()` call. The
pool is stored on `app.state.ferrum_conn` and closed via `Connection.close()`
which calls `await self._lifecycle.wait_drained(timeout=...)`
(`python/ferrum/connection.py:322`, `_EventLifecycleGuard` at `:105`,
`wait_drained` at `:133-142`) — event-based drain, not busy polling.

All W1-E knobs thread through: `acquire_timeout`, `query_timeout`,
`statement_timeout`, `max_lifetime`, `max_idle_lifetime`,
`max_connection_age`, `command_timeout`, `statement_cache_size`, `ssl`,
`server_settings`, `application_name`, `drain_timeout`, `echo`. Verified
against `connect()`'s signature (`connection.py:1043-1110`).

The integration test `test_fastapi_one_pool_serves_concurrent_requests`
(5 simultaneous POSTs against `max_size=5`) confirms no serialization.

### C2 — Transaction-scoped dependencies (source inspection)

`get_ferrum_transaction` (`python/ferrum/contrib/fastapi.py`, diff):

```python
async def get_ferrum_transaction(request: _StarletteRequest) -> AsyncGenerator[Transaction, None]:
    conn = getattr(request.app.state, "ferrum_conn", None)
    if not isinstance(conn, Connection):
        raise RuntimeError(...)
    async with conn.transaction() as tx:
        yield tx
```

This is the canonical FastAPI yield-dependency pattern:
`async with conn.transaction() as tx: yield tx` — the `Transaction.__aexit__`
commits on clean exit, rolls back on exception. The transaction pins one
pooled connection for the request, matching ratified §5a: "statements
issued through a `Transaction` never retry." The separation from
`get_ferrum_conn` (autocommit reads) is architecturally clear and correct.

`update_instance` signature verified at `python/ferrum/queryset.py:3459`:
`async def update_instance(self, conn: ConnectionLike, obj: _M, *, fields: Sequence[str] | None = None) -> int`.
The `FerrumUserDatabase.update` call matches.

### C3 — Error translation architecture (source inspection)

Three-layer design, all in `python/ferrum/contrib/fastapi.py`:

1. `map_ferrum_to_http_status(exc)` — class-based mapping, never inspects
   `str(exc)`. `FerrumNotFoundError`→404, `FerrumIntegrityError`/
   `FerrumMultipleObjectsError`→409, `FerrumCompileError`/
   `FerrumRelationNotLoadedError`→400, `FerrumConfigError`/
   `FerrumTimeoutError`/`FerrumConnectionError`→503, base→500. The
   unimported subclasses (`FerrumSchemaError`, `FerrumHydrationError`,
   `FerrumInternalError`, `FerrumMigrationError` — all confirmed present
   in `python/ferrum/errors.py:271,282,293,441` as `FerrumError` subclasses)
   fall through to `return 500`. Correct.

2. `_sanitize_ferrum_error_payload(exc)` — builds `dict` with exactly
   `code`, `category`, `sqlstate`, `constraint`, `model`, `operation`,
   omitting `None` fields. Never reads `str(exc)`. Matches ratified §5a
   safe-error-field set exactly.

3. `ferrum_exception_handler(request, exc)` — lazy-imports
   `starlette.responses.JSONResponse`, returns it with the sanitized
   payload. `register_ferrum_exception_handlers(app)` registers for
   `FerrumError` (base class) — Starlette dispatch uses `isinstance`, so
   all subclasses are caught.

The independent verification (`verification/20260829T091235Z.md` §C3)
ran an adversarial probe with crafted exceptions carrying DSN, DETAIL,
email, password, and row data in `str(exc)`. Confirmed none leak into the
JSON body. This satisfies §3 (credential handling, error boundaries) and
§5a (safe error fields).

### C4 — fastapi-users adapter design (source inspection)

`FerrumUserDatabase.__init__` calls `_import_fastapi_users_db_protocol()`
which soft-imports `fastapi_users.db.base.BaseUserDatabase` and raises a
clear `ImportError` with an actionable message if missing. The adapter is
constructed with host-supplied `user_model` and optional
`oauth_account_model` (both `Any` — acceptable for an adapter pattern where
the host supplies Pydantic models).

Each method takes an explicit `Connection | Transaction` argument — the
caller controls transaction boundaries. This is consistent with §5a's
connection-explicit design ("QuerySet stays shard-unaware and
connection-explicit"). Methods: `get_by_id`, `get_by_email`,
`get_by_username`, `create`, `update`, `delete`, `add_oauth_account`,
`update_oauth_account`, `get_by_oauth_account`. Error translation:
`FerrumIntegrityError`→`UserAlreadyExists`, `FerrumNotFoundError`→`None`
(for lookups). `get_by_username` catches `FerrumCompileError` (model has
no `username` field) → `NotImplementedError` with a clear message.

`pyproject.toml` does NOT declare `fastapi-users` (verified by
`grep -n "fastapi-users\|fastapi_users" pyproject.toml` → no matches in
the verification record). No new hard dependency; the shared
`pyproject.toml` was not touched (not an owned path).

### C5 — Import boundary (independent confirmation)

`.importlinter` contracts (read directly):
- `cli-isolation` (forbidden): source `ferrum.models`, `ferrum.queryset`,
  `ferrum.connection`, `ferrum.hooks`, `ferrum.errors`, `ferrum.migrations`
  must not import `ferrum.cli`, `ferrum.contrib`, `typer`, `rich`,
  `fastapi`, `starlette`.
- `contrib-isolation` (forbidden): core modules + `ferrum.cli` must not
  import `ferrum.contrib`.

`ferrum.contrib.__init__.py` (diff): docstring + `__all__: list[str] = []`.
No imports of `fastapi`, `starlette`, or any contrib submodule. Confirmed
by reading the diff and by the verification's clean-room subprocess probe
(`import ferrum.contrib` → `leaked: []`).

The eager `_StarletteRequest` import is confined to
`ferrum.contrib.fastapi` (not `ferrum.contrib.__init__`). The `Any`
fallback (`_StarletteRequest = Any` on `ImportError`) keeps the module
importable without Starlette, preserving `import ferrum.contrib.fastapi`
as a non-failing operation even without the extra — only `Depends` usage
requires Starlette. This is the correct boundary: the contrib module may
import its framework; the package `__init__` and all core modules may not.

`mise run import-boundary` (fresh run in verification): 0 broken contracts.

### Architectural constraint compliance

| §2 constraint | Status |
|---|---|
| §2.1 Python owns public developer ergonomics | Lifespan, deps, error translation all Python-side. ✓ |
| §2.3 Async-first only | All public APIs are `async`. ✓ |
| §2.5 PyO3 + maturin boundary | Not touched by W2-D. N/A. ✓ |
| §2.9 No raw SQL escape hatches | Adapter uses Ferrum QuerySet terminals only. ✓ |
| §2.10 No per-request mutable shared state in Rust | Adapter doesn't touch Rust. ✓ |

| §3 security rule | Status |
|---|---|
| Credential handling | DSN never logged; `ferrum_lifespan` docstring states CRED-1. ✓ |
| Tiered observability | Error translation emits only Tier A safe fields. ✓ |
| Error boundaries | `map_ferrum_to_http_status` + sanitized payload; no `DETAIL`/`HINT`/row data. ✓ |

| §5a contract | Status |
|---|---|
| Object-scoped retry-disable | `get_ferrum_transaction` pins one connection per request; statements through the `Transaction` never retry (inherited from `Transaction` construction). ✓ |
| Safe error fields | Payload carries exactly `sqlstate`/`category`/`constraint`/`model`/`operation` (+ `code`). ✓ |
| Connection-explicit QuerySet | `FerrumUserDatabase` methods take explicit `Connection \| Transaction`. ✓ |

## Findings

No blocking findings. All five architectural claims (C1–C5) are satisfied.

### Non-blocking observations

1. **`_StarletteRequest` eager import deviation (justified).** FastAPI's
   dependency injection requires the concrete `starlette.requests.Request`
   type to recognize the parameter as the ASGI request; a `Protocol` is
   rejected. The eager import is confined to `ferrum.contrib.fastapi`
   (not `ferrum.contrib.__init__`), so the import boundary is preserved.
   The `Any` fallback keeps the module importable without Starlette. This
   matches the existing `ferrum[fastapi]` extra contract. No action
   required.

2. **`FerrumUserDatabase` uses `Any` for model types.** Acceptable for an
   adapter where the host supplies Pydantic models. A future improvement
   could use a `TypeVar` bound to `ferrum.Model` for tighter static
   typing, but this is a contrib-ergonomics enhancement, not an
   architectural defect.

3. **Integration tests use raw ASGI, not `TestClient`.** Avoids adding
   `httpx` as a dev dependency (shared path). The raw ASGI approach is
   sufficient to prove lifespan, dependency, error-translation, and
   concurrency contracts. Not blocking.

4. **`fastapi-users` is not a declared dependency.** The adapter
   soft-imports it; consumers pin their own compatible version. A future
   `pyproject.toml` change (shared path) could add an optional
   `fastapi-users` extra. This is a product/packaging decision, not an
   architecture decision; out of ChiefArchitect scope and correctly
   deferred.

5. **`ferrum_lifespan` raises `FerrumTimeoutError` on drain timeout.**
   This surfaces as a lifespan shutdown error to the ASGI server — correct
   behavior (log/alert). No connection leak (pool is still closed).

## Decision

**approved**

The FastAPI integration architecture is sound. It respects every
non-negotiable constraint in §2, every security rule in §3, and every
ratified §5a contract that applies (object-scoped retry-disable,
safe-error-field set, connection-explicit QuerySet). The one-pool-per-
process lifespan, transaction-scoped dependencies, error translation
layering, optional adapter soft-import design, and import boundary are
all architecturally correct. The documented deviations
(`_StarletteRequest` eager import, raw-ASGI tests, `Any` model types)
are justified and confined to the contrib layer.

This record grants only the ChiefArchitect gate. It does not substitute
for SecurityEngineer (auth/credential handling — required) or
CodeReviewer (general code quality — required) gates.
