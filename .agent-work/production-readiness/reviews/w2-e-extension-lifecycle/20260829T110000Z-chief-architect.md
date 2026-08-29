---
task_id: w2-e-extension-lifecycle
run_id: 20260829T110000Z
authority: ChiefArchitect
reviewer: chief-architect-agent
reviewed_at: 2026-08-29T11:30:00Z
base_revision: 87f39966d60303b30943308c9123418d9d47252e
decision: approved
scope:
  - python/ferrum/drivers/protocol.py
  - python/ferrum/ext/pgvector.py
  - python/ferrum/ext/__init__.py
  - tests/python/unit/test_pgvector.py
  - tests/python/integration/test_pgvector_lifecycle.py
---

# Named Authority Verdict

## Authority

ChiefArchitect

## Claims reviewed

1. **ConnectionInitializer protocol architecture** — a `@runtime_checkable`
   Protocol in `drivers/protocol.py` with a `name: str` attribute and an
   `async def initialize(conn: ConnectionLike) -> None` method, plus a
   `ConnectionLike = Any` structural alias that preserves the import boundary
   (no import of `ferrum.connection`).
2. **PgVectorInitializer design** — a declarative class implementing
   `ConnectionInitializer` that validates the backend, runs `CREATE EXTENSION IF
   NOT EXISTS vector`, tolerates `DuplicateObjectError`, registers the vector
   codec pool-wide via `driver.add_type_codec(...)`, is idempotent, and fails
   closed on permanent errors.
3. **Pool growth / reconnect initializer mechanism** — the codec registration
   survives pool growth and `expire_connections()` because `add_type_codec`
   appends to the driver's `_extra_codecs` list and the pool `init` callback
   (`_init_conn`) re-applies every codec on each new pooled connection.
4. **Generalized codec mechanism for citext / consumer-defined initializers** —
   the `ConnectionInitializer` protocol is driver-agnostic; a consumer-defined
   `CitextInitializer` can implement it and use the sanctioned
   `conn._require_driver()` seam.
5. **No raw connection exposure** — initializers consume the Ferrum
   `Connection` surface; consumer-defined initializers are guided to
   `conn._require_driver()`, never the raw asyncpg pool.
6. **DEFECT-1 / DEFECT-2 corrections** (corrective run 20260829T110000Z) —
   docstrings now accurately describe the `pool.execute` path and the `name`
   attribute's non-enforced nature.

## Evidence

### Protocol architecture (`drivers/protocol.py`)

`git diff HEAD -- python/ferrum/drivers/protocol.py` shows a 52-line append
after the existing `DriverProtocol` class (lines 94-143). The added block:

- `ConnectionLike = Any` (line 103) — structural alias, no `ferrum.connection`
  import. Import boundary preserved.
- `@runtime_checkable class ConnectionInitializer(Protocol)` (lines 106-143)
  with `name: str` and `async def initialize(self, conn: ConnectionLike) -> None`.
- `from typing import Any, Protocol, runtime_checkable` was already imported
  (line 12); no new imports needed.

The protocol is a pure Python declarative contract. It does not perform I/O;
it describes the shape a driver/pool consumes from its `init` callback. This
satisfies §2 constraint 3 (async-first: `initialize` is async) and constraint 10
(no per-request mutable shared state in Rust — this is Python-only).

The `ConnectionLike = Any` alias is pragmatic: it keeps the protocol module
free of a `ferrum.connection` import (which would create a cycle), while the
docstring documents that implementations consume the `Connection` surface
(`conn.dialect`, `conn._require_driver()`, `conn._driver`).

### PgVectorInitializer design (`ext/pgvector.py`)

`python/ferrum/ext/pgvector.py:120-214` defines `PgVectorInitializer`:

- `name = "pgvector"` (line 159) — diagnostic label.
- `__init__(*, timeout: float = 5.0)` (line 161) — keyword-only, explicit.
- `async def initialize(self, conn: ConnectionLike) -> None` (line 164):
  1. Validates `conn.dialect == "postgres"` → `FerrumConfigError` otherwise.
  2. Reads `driver = conn._driver`, `pool = getattr(driver, "_pool", None)`;
     raises `FerrumConfigError` if either is `None`.
  3. `await pool.execute("CREATE EXTENSION IF NOT EXISTS vector", timeout=...)`
     — hard-coded DDL, no user-supplied identifiers (§2 constraint 9 satisfied).
  4. Catches exceptions whose type name contains `"DuplicateObject"` and
     swallows them (SQLSTATE 42710 concurrent startup race). All other
     exceptions re-raise — fail-closed.
  5. `await driver.add_type_codec("vector", schema="public", encoder=...,
     decoder=..., format="text")` — pool-wide codec registration.

The fail-closed contract is architecturally correct: a pool that silently
served queries against unregistered vector columns would produce
non-deterministic `DataError` depending on which pooled connection served the
query. Propagating the `CREATE EXTENSION` failure prevents this.

### Pool growth / reconnect mechanism (`drivers/postgres.py`)

Inspected `python/ferrum/drivers/postgres.py:253-337` (not modified by W2-E;
W1-E owns):

- `_extra_codecs: list[dict[str, Any]]` (line 253) — driver-level codec list.
- `_init_conn` callback (lines 285-293): iterates `extra_codecs` and calls
  `conn.set_type_codec(**codec)` on every new pooled connection. This is the
  pool `init` hook (`pool_kwargs["init"] = _init_conn`, line 295).
- `add_type_codec` (lines 306-337): appends to `_extra_codecs` (deduplicated
  by typename+schema), then calls `self._pool.expire_connections()` to force
  already-initialized connections to be replaced.

The mechanism is sound: pool growth (connections opened beyond `min_size`)
automatically get the codec from `_init_conn`; `expire_connections()` forces
new connections that also get the codec. Integration tests
`test_initializer_survives_pool_growth` and
`test_initializer_survives_expire_connections` verify this against live
PostgreSQL.

### Generalized mechanism

The `ConnectionInitializer` protocol is driver-agnostic (consumes
`ConnectionLike`, not `AsyncpgDriver`). The unit test
`test_citext_style_initializer_uses_sanctioned_seam` demonstrates a
`CitextInitializer` using `conn._require_driver().execute(...)`.
`test_composing_initializers_runs_in_sequence` shows multiple initializers
compose in sequence. The protocol is the correct generalization surface.

### No raw connection exposure

The canonical `PgVectorInitializer` reaches `conn._driver._pool.execute(...)`
directly — a private attribute access on the asyncpg pool. The corrective run
(20260829T110000Z) fixed the docstrings to accurately describe this path
(DEFECT-1). The protocol docstring (lines 117-122) now correctly distinguishes:

- The canonical `PgVectorInitializer` reaches the asyncpg pool directly
  (`conn._driver._pool.execute`) to preserve the legacy
  `register_vector_codecs` behavior.
- Consumer-defined initializers may use `conn._require_driver()`.

This is an acceptable architectural trade-off: the pgvector initializer is a
Ferrum-shipped extension that knows the asyncpg driver internals (same as the
legacy `register_vector_codecs` it replaces); consumer-defined initializers
are guided to the public `conn._require_driver()` seam. The protocol contract
documents the distinction explicitly.

A future hardening pass could route `CREATE EXTENSION` through
`conn._require_driver().execute(...)` once the `test_pgvector_score.py` mocks
are updated (shared path, requires a coordinator lease). This is a follow-up,
not a blocker.

### DEFECT-1 / DEFECT-2 corrections verified

Current source (after corrective run 20260829T110000Z):

- `ext/pgvector.py:126-130`: "Runs `CREATE EXTENSION IF NOT EXISTS vector`
  directly against the asyncpg pool (`conn._driver._pool.execute(...)`),
  preserving the behavior of the legacy `register_vector_codecs` helper." ✅
- `protocol.py:94-102`: comment block accurately describes the `pool.execute`
  path for pgvector and `conn._require_driver()` for consumer-defined. ✅
- `protocol.py:115-122`: "must" list accurately describes both paths. ✅
- `protocol.py:137-138`: "`name` is a diagnostic label for logging and error
  messages; it is not enforced by `@runtime_checkable` (which only verifies
  method presence)." ✅

### `connect(..., extensions=...)` not wired

The task contract explicitly excludes `connection.py` ("do NOT modify
`connect()`"). The initializer protocol is the contract that `connect()` would
consume; the module docstring documents the intended usage. Wiring it through
`ferrum.connect()` requires a shared-path lease on `connection.py`
(coordinator-managed). The protocol is ready to consume when that lease is
granted. This is a tracked follow-up, not a W2-E scope gap.

### Lint / type / tests

```
$ uv run ruff check python/ferrum/ext/pgvector.py python/ferrum/ext/__init__.py \
    python/ferrum/drivers/protocol.py tests/python/unit/test_pgvector.py
All checks passed!

$ uv run ruff format --check python/ferrum/ext/pgvector.py python/ferrum/ext/__init__.py \
    python/ferrum/drivers/protocol.py tests/python/unit/test_pgvector.py
4 files already formatted

$ ty check python/ferrum/ext/pgvector.py python/ferrum/ext/__init__.py \
    python/ferrum/drivers/protocol.py
All checks passed!

$ uv run pytest tests/python/unit/test_pgvector.py -x -q
21 passed in 0.19s
```

## Findings

| ID | Severity | Evidence | Required correction |
|----|----------|----------|----------------------|
| ARCH-1 | info (follow-up) | `connect(..., extensions=...)` is documentation-only; wiring requires a shared-path lease on `connection.py`. The initializer protocol is ready to consume. | Track as a follow-up task; not a W2-E blocker. |
| ARCH-2 | info (follow-up) | The canonical `PgVectorInitializer` reaches `conn._driver._pool.execute(...)` directly (private attribute), preserving legacy behavior. Consumer-defined initializers use `conn._require_driver()`. | Optional future hardening pass to route through `conn._require_driver().execute()` once `test_pgvector_score.py` mocks are updated (shared path). |
| ARCH-3 | info | `ConnectionLike = Any` is a structural alias. A stricter `Protocol` for `Connection` would improve type safety but require importing `ferrum.connection` (cycle). The current trade-off is documented and acceptable. | None. |

No blocking findings. All §2 constraints (2, 3, 7, 9, 10) are satisfied. The
protocol architecture is sound, the pool-growth/reconnect mechanism is the
correct pattern, the generalization surface is driver-agnostic, and the
DEFECT-1/DEFECT-2 corrections make the docstrings accurate.

## Decision

**approved**

This record grants only the ChiefArchitect gate. It does not substitute for
the CodeReviewer gate or independent verification.
