# Rule: Async

> Ferrum is **async-first only**. There is no synchronous API in the v0.1 MVP. See `AGENTS.md` §2.

## Hard constraints

- **No sync API.** No synchronous public methods, sync wrappers, blocking compatibility layer,
  or hidden `loop.run_until_complete` shims in library code. Every core operation is awaitable.
- **I/O is Python-side and async.** All database I/O happens through the async driver
  (`asyncpg`-leaning per ADR-001) at a Python await point. Rust never performs or waits on I/O.
- **Cancellation safety.** Operations must be cancellation-safe. A cancelled `await` must not
  leave a connection in a half-consumed state or a transaction silently open. Always release
  pooled resources on cancellation.
- **Timeouts at the await point.** Query/statement timeouts and cancellation are enforced in
  Python around the driver await — never inside Rust compilation.

## Concurrency & failure modes (call these out explicitly)

- **Connection pool:** acquisition is bounded and awaitable; document and bound pool-exhaustion
  behavior. Never hold a pooled connection across an unbounded await.
- **Transactions:** transaction scope is explicit and tied to an async context manager; a task
  cancellation inside a transaction must roll back deterministically.
- **Backpressure:** streaming/cursor results must respect consumer backpressure and not buffer
  unbounded rows.
- **Event-loop discipline:** do not block the event loop with CPU-bound work other than the
  sub-millisecond GIL-holding Rust compile call.

## Definition of done (async)

- [ ] All new public surface is awaitable; no sync path introduced.
- [ ] Cancellation and timeout behavior is tested for the touched operation.
- [ ] Pooled connections and transactions are released/rolled back on cancellation.
- [ ] No unbounded buffering on streaming paths.
