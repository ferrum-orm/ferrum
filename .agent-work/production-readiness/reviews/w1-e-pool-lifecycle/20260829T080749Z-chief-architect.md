---
task_id: w1-e-pool-lifecycle
run_id: 20260829T080749Z
authority: ChiefArchitect
reviewer: chief-architect
reviewed_at: 2026-08-29T11:30:00Z
base_revision: 02d585513980ae89dbd6474196619a82faac460d
decision: approved
scope:
  - PoolStats/shutdown contract
  - Pool lifecycle architecture (acquire_timeout, idle vs max age, failover replacement)
  - No new components or failure modes beyond task contract
  - PyO3 boundary (§4) preserved
---

# Named Authority Verdict

## Authority

ChiefArchitect

## Claims reviewed

1. **PoolStats typed dataclass**: `PoolStats` is a typed `@dataclass(frozen=True)`
   with the required fields: `size`, `idle`, `acquired`, `waiters`, `min_size`,
   `max_size`, `inflight`, `accepting`, `closing`.
2. **Event-based shutdown**: `close()` uses `asyncio.Event` (not busy-polling);
   drain timeout is reported; active streams are closed; no connection leak.
3. **`acquire_timeout` on every acquire path**: `fetch`, `fetchrow`, `fetchval`,
   `execute`, `transaction()`, `open_stream()`, and `acquire()` all forward the
   timeout.
4. **Distinct idle lifetime vs hard max age**: `max_idle_lifetime` (idle
   recycling) and `max_connection_age` (hard max age) are separate config knobs
   with distinct semantics.
5. **No new components or failure modes** beyond what the task contract
   specifies.
6. **PyO3 boundary (§4) preserved**: no Rust changes, no async in Rust.

## Evidence

### C1 — PoolStats typed dataclass

`python/ferrum/drivers/postgres.py:151-187` — `@dataclass(frozen=True)` with
exactly:

```python
size: int
idle: int
acquired: int
waiters: int
min_size: int
max_size: int
inflight: int
accepting: bool
closing: bool
```

All fields from the task contract ("size, idle, acquired, waiters if
available, min/max, in-flight, accepting/closing") are present. `frozen=True`
makes the snapshot immutable. `waiters` is `-1` when asyncpg does not expose
the count — documented in the docstring. The task contract says "waiters if
available," so the sentinel is compliant. `Connection.pool_stats()`
(`connection.py:358-373`) combines driver pool internals with Ferrum lifecycle
state (`inflight`, `accepting`) via a clean delegation — no architecture
boundary violation.

### C2 — Event-based shutdown

`python/ferrum/connection.py:101-145` — `_EventLifecycleGuard` subclasses
`_LifecycleGuard` from `runtime.py` (NOT owned by W1-E, NOT modified). Uses
`asyncio.Event` (`_drained`):

- `begin()` clears the event (work in progress).
- `end()` sets the event when `_inflight == 0` (all work done).
- `wait_drained(timeout=...)` uses `asyncio.wait_for(self._drained.wait(),
  timeout=timeout)` — a single event await, not a sleep-poll loop.

`close()` (`connection.py:307-336`):
1. `stop_accepting()` — rejects new work.
2. `close_streams()` — force-closes active streams (inherited from base class,
   `runtime.py:129-135`).
3. `wait_drained(timeout=drain_timeout)` — event-based wait.
4. `await self._driver.close()` — always executes (no leak).
5. `finally: self._driver = None` — always resets.
6. If `not drained`: raises `FerrumTimeoutError(category="timeout")` with
   abandoned count and timeout value — AFTER the pool is closed.

The ordering is architecturally correct: pool is always closed, then the
timeout is reported. No connection leak on the drain-timeout path.

`AsyncpgDriver.close()` (`postgres.py:340-352`) uses `pool.terminate()` when
connections are still acquired (drain-timeout path) to avoid a deadlock;
otherwise `await pool.close()`. The `terminate()` fallback accesses asyncpg
private attributes (`_holders`, `_in_use`) defensively via `getattr` — a
fragility risk documented in the executor log, acceptable for v0.1.

The old `drain_inflight` import is removed from `connection.py` (line 41).
No busy-polling remains.

### C3 — acquire_timeout on every acquire path

`python/ferrum/drivers/postgres.py`:

- `_acquire_cm()` (lines ~388-396): single seam returning
  `pool.acquire(timeout=self._acquire_timeout)` when set, else `pool.acquire()`.
- `fetch` (line 416), `fetchrow` (line 423), `fetchval` (line 430), `execute`
  (line 426): all use `async with self._acquire_cm() as raw_conn`.
- `transaction()` (lines 499-511): direct `pool.acquire(timeout=...)` with
  `if self._acquire_timeout is not None` branch.
- `open_stream()` (lines 444-451): direct `pool.acquire(timeout=...)` with
  the same branch — verified by reading the full method (not just diff
  context).
- `acquire()` context manager (lines 464-466): same branch.

Every acquire path forwards the timeout. No path calls `pool.acquire()`
without the timeout branch when `_acquire_timeout` is set.

### C4 — Distinct idle lifetime vs hard max age

`python/ferrum/drivers/postgres.py:223-244`:

- `max_idle_lifetime`: maps to asyncpg's
  `max_inactive_connection_lifetime` (idle recycling). Legacy `max_lifetime`
  aliases this (`max_idle_lifetime if max_idle_lifetime is not None else
  max_lifetime`).
- `max_connection_age`: hard max age — tracked via `_conn_birth_times` in the
  init callback (line 291: `driver_ref._conn_birth_times[id(conn)] =
  time.monotonic()`) and enforced via `expire_connections()`.

These are distinct config knobs with distinct semantics: one is idle
recycling (asyncpg-managed), the other is hard age limit (Ferrum-tracked).
The enforcement for `max_connection_age` is best-effort (manual or
failover-triggered `expire_connections()`, not automatic acquire-time
enforcement). This is documented as the minimal implementation and is
acceptable for v0.1 — the task contract requires the *distinction*, which
exists.

### C5 — No new components or failure modes beyond task contract

Inspected against the task contract's acceptance criteria:

- `_EventLifecycleGuard` — new class in `connection.py` (owned), subclasses
  `_LifecycleGuard` from `runtime.py` (not owned, not modified). This is the
  specified "event/condition shutdown instead of busy polling" approach, not
  a new component. The inheritance extends behavior without modifying the
  shared base class.
- `_acquire_cm()` — refactor to centralize timeout logic, not a new component.
- `_handle_post_error` / `_expire_connections_safe` — implement the specified
  "failover-safe validation/replacement; no unconditional pre-ping" criterion.
  The failover-category check (`getattr(mapped, "category", None) == "failover"`)
  delegates to W1-D's error taxonomy. Not a new failure mode — it's the
  specified replacement path.
- `pool_stats()` method — specified by the acceptance criterion.
- `expire_connections()` public method — mechanism for failover replacement
  and `max_connection_age` enforcement, both in scope.
- `Transaction` fallback lifecycle changed from `_LifecycleGuard()` to
  `_EventLifecycleGuard()` (line 617). In practice `Transaction` always
  receives a parent lifecycle from `Connection.transaction()`. The fallback
  is strictly better (event-based vs busy-poll). Not a new failure mode.

No new components or failure modes introduced beyond the task contract.

### C6 — PyO3 boundary (§4) preserved

`git diff --name-only HEAD` — no `crates/` files in W1-E scope. The
`crates/ferrum-sql/src/emit.rs` change is from W1-A (query correctness),
confirmed by the verifier (C10) and by the task contract ownership list. All
W1-E changes are in `python/ferrum/drivers/postgres.py` and
`python/ferrum/connection.py` — pure Python, async I/O path. No Rust changes,
no async in Rust. The PyO3 boundary is untouched.

### C7 — Non-PostgreSQL driver compatibility

`Connection.open()` (`connection.py:266-290`) checks the DSN scheme and only
passes new kwargs (`max_idle_lifetime`, `max_connection_age`,
`command_timeout`, `statement_cache_size`, `ssl`, `server_settings`,
`application_name`) to PostgreSQL drivers. Other drivers receive only the
original kwargs. This does not introduce dialect-switching (rejected in §5a) —
it's a scheme check that gates kwargs, not a routing layer.

## Findings

### F1 — Minor: no integration test for `open_stream` + `acquire_timeout`

- Severity: minor (test coverage, not implementation)
- Evidence: The verifier noted this (F1). The implementation enforces
  `acquire_timeout` on `open_stream` (`postgres.py:444-451`, verified by
  reading the full method), but there is no integration test that saturates
  the pool and verifies `open_stream` honors the timeout.
- Required correction: none for this gate. The implementation is correct and
  the acceptance criterion is satisfied at the implementation level. A
  follow-up test can be added by CodeReviewer or a future task.

### F2 — Note: `pool.terminate()` accesses asyncpg private attributes

- Severity: informational (documented fragility)
- Evidence: `AsyncpgDriver.close()` (`postgres.py:340-352`) accesses
  `pool._holders` and `h._in_use` via `getattr` to decide between
  `pool.terminate()` and `pool.close()`. This is defensive against asyncpg
  internal changes but depends on private attributes.
- Required correction: none for v0.1. Documented in the executor log. If
  asyncpg changes these internals, the `getattr` defaults fall through to
  `pool.close()` (the graceful path), which may deadlock if connections are
  still acquired — but that path only fires after a drain timeout, which is
  already an error condition.

### F3 — Note: `max_connection_age` enforcement is best-effort

- Severity: informational (documented limitation)
- Evidence: Birth times are tracked in the init callback, but enforcement is
  via `expire_connections()` (manual or failover-triggered), not automatic
  acquire-time enforcement.
- Required correction: none for v0.1. The task contract requires the
  *distinction* between idle lifetime and hard max age, which exists. More
  aggressive enforcement can be added if benchmarks justify it.

## Decision

`approved`

The PoolStats/shutdown contract and pool lifecycle architecture meet all
task contract acceptance criteria and honor the architectural constraints in
§2 (non-negotiable), §4 (PyO3 boundary), and §5a (no new components or failure
modes beyond ratified contracts). The implementation is minimal, scoped to
owned paths, and introduces no speculative complexity. `_EventLifecycleGuard`
correctly extends the base class without modifying the shared `runtime.py`.
The `close()` ordering guarantees no connection leak on the drain-timeout
path. `acquire_timeout` is enforced on every acquire path. `PoolStats` is a
properly typed frozen dataclass with all required fields.

This record grants only the ChiefArchitect gate for the PoolStats/shutdown
contract and pool lifecycle architecture. It does not substitute for the
SecurityEngineer (TLS/DSN redaction) or CodeReviewer (code quality) gates.
