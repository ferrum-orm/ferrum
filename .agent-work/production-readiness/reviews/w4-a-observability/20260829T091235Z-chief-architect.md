---
task_id: w4-a-observability
run_id: 20260829T091235Z
authority: ChiefArchitect
reviewer: chief-architect
reviewed_at: 2026-08-29T11:30:00Z
base_revision: b5e7ed3beaab60b7ded6ff6b1f8b77293ad376bb
decision: approved
scope:
  - python/ferrum/observability.py
  - python/ferrum/hooks.py
---

# Named Authority Verdict

## Authority

ChiefArchitect — observability architecture (span model, metric
cardinality, fingerprint opt-in, pool lifecycle event contract, Prometheus
rendering, W1-D Tier-A hook contract preservation).

## Claims reviewed

1. One span around actual query execution (start → success/failure pairing
   via contextvars), NOT zero-duration event spans. Ambient parent context.
2. Low-cardinality metrics from Tier-A-safe fields only; bounded label
   cardinality.
3. Query fingerprints NOT in default metric labels; opt-in exemplars only.
4. Pool acquire/release/wait/timeout/shutdown events from real lifecycle
   points (handlers + hook helpers ready).
5. Prometheus text-exposition rendering.
6. W1-D Tier-A hook contract and redaction layer preserved.

## Evidence

### Source inspection (direct, not via executor/verifier summary)

**Span-around-query** — `python/ferrum/observability.py:363-400`:
- `query_start` opens ONE span via `tracer.start_span("ferrum.query")` and
  stores it in `_active_query_span` contextvar (lines 370-371).
- `query_success`/`query_failure` retrieve the span, set attributes, record
  counters/duration, and call `span.end()` in a `finally` block (lines
  374-400). Span duration = query duration from the success/failure payload.
- `tracer.start_span` (no `context=` arg) uses the ambient parent context
  per the OTel API contract — children of FastAPI middleware spans.
- `contextvars.ContextVar` is asyncio-task-scoped: concurrent requests in
  different tasks get independent span values; within one task,
  query_start → await driver.fetch → query_success is sequential, so the
  span is naturally scoped to one execution. Architecturally sound.
- Defensive fallback (lines 378-382): orphaned success/failure without a
  matching start creates a zero-duration fallback span so the event is
  still observed. This is graceful degradation, NOT the zero-duration
  event-span anti-pattern that the rewrite replaced.

**Low-cardinality metrics** — `observability.py:112-128` (`_safe_labels`):
- Labels limited to `operation`, `status`, `category`, `failure_category`,
  `isolation`, `direction` — all low-cardinality enum/string fields.
- Pool snapshot fields (`pool_size`, `pool_idle`, `pool_acquired_count`,
  `pool_waiters`) are recorded as metric VALUES (gauges), NOT labels
  (`observability.py:188-194`). Correct — avoids cardinality explosion from
  varying pool sizes.
- `fingerprint` excluded unless `include_fingerprint=True`.

**Fingerprint opt-in** — `observability.py:43,98,124,142,152,163`:
- `_EXEMPLARS_ENABLED` defaults to `False` (line 43).
- `enable_exemplars()` sets it `True` (line 98); `disable_exemplars()` resets
  (line 104).
- `_safe_labels()` includes `fingerprint` only when `include_fingerprint`
  is True (line 124).
- `_on_query_success`, `_on_query_failure`, `_on_hydration_failure` pass
  `include_fingerprint=_EXEMPLARS_ENABLED` (lines 142, 152, 163).

**Pool lifecycle events** — `hooks.py:307-499`, `observability.py:178-203`:
- Hook helpers `pool_acquire`, `pool_release`, `pool_wait`, `pool_timeout`,
  `pool_shutdown` dispatch Tier-A-only payloads (integer snapshots).
- `_on_pool_event` handler maps 5 events to counters + gauges.
- `grep` for dispatch calls in `connection.py`, `runtime.py`,
  `drivers/postgres.py` returned NO matches — dispatch sites are absent.
  This matches the executor's flagged residual risk: `connection.py` /
  `runtime.py` are W1-E owned paths, not W4-A. The handlers/helpers are the
  contract future W1-E dispatch sites will call.

**Prometheus rendering** — `observability.py:466-542`:
- `render_prometheus()` emits `# HELP` / `# TYPE` lines with dot→underscore
  name conversion, followed by sample lines. Text-exposition format
  correct.

**W1-D Tier-A hook contract** — `hooks.py:43-107,212-306`:
- `_TIER_A_KEYS` (lines 43-70): original 10 keys preserved (`event`, `model`,
  `table`, `operation`, `fingerprint`, `duration_ms`, `status`,
  `failure_category`, `category`, `rows_affected`). 9 new keys added AFTER,
  original set untouched — additive only.
- `query_failure()` (lines 212-240): accepts `category: str | None = None`,
  includes `category` when not None. **UNCHANGED.**
- `hydration_failure()` (lines 243-272): accepts `category: str | None =
  None`. **UNCHANGED.**
- `QueryTimer.__exit__` (lines 288-306): extracts `category = getattr(exc_val,
  "category", None)`. **UNCHANGED.**
- `_redact()` (lines 86-107): non-bypassable; strips all non-Tier-A keys
  before any hook receives data. Tier A/B/C model preserved. The 9 new
  keys are in `_TIER_A_KEYS` so they survive Tier-A redaction — intended
  behavior, since they are enums/counts/durations/booleans, never bound
  values.

### Commands (architecture-relevant, from executor + verifier logs)

- `uv run pytest tests/python/unit/test_observability.py
  tests/python/unit/test_hooks_observability.py tests/python/unit/test_hooks.py
  -x -q` → 67 passed. W1-D preservation tests pass.
- Live PostgreSQL integration: 5 passed (span, cardinality, redaction,
  integrity error, exemplars).
- Security tests: 144 passed (telemetry redaction, no secrets/DSNs/values).
- `uv run ty check python/ferrum/observability.py python/ferrum/hooks.py`
  → All checks passed.

## Findings

| # | Severity | Finding | Required correction |
|---|----------|---------|---------------------|
| 1 | LOW (architectural consistency, follow-up) | `transaction_start` creates a zero-duration span (`observability.py:410-412`: `with tracer.start_span(...): pass`) — the same anti-pattern W4-A fixed for queries. For trace consistency, transaction spans should pair start→end via a contextvar like the query span. | Future W1-E follow-up that instruments transaction dispatch sites should pair `transaction_start`→`transaction_end` into one span. Not a W4-A blocker: the task contract scopes span-around to query execution; transaction dispatch sites aren't instrumented yet so these spans don't fire. |
| 2 | LOW (residual risk, correctly flagged, not a defect) | Pool/transaction/migration/retry/timeout dispatch sites absent from `connection.py`/`runtime.py`/migrations. Handlers and hook helpers are ready but dormant. | Future W1-E follow-up must add `hooks.pool_acquire()` etc. calls at real lifecycle points (`Connection.acquire`, `Connection.close`, `_LifecycleGuard.begin/end`, `TimedQueryExecutor._run`). Correctly scoped out of W4-A owned paths. |
| 3 | INFO (SecurityEngineer gate, not ChiefArchitect) | `_TIER_A_KEYS` extended with 9 new keys (`pool_size`, `pool_idle`, `pool_acquired_count`, `pool_waiters`, `isolation`, `readonly`, `deferrable`, `direction`, `attempt`). Architecturally safe: all are enums, integer counts, durations, or booleans — never bound values, DSNs, credentials, or row data. Cardinality bounded by enum domains (e.g. `direction` ∈ {"up","down"}, `isolation` ∈ {4 + "default"}). | SecurityEngineer review is mandatory for the `errors_redaction` surface (`security_review: true` in task contract). ChiefArchitect confirms the keys are architecturally sound for low-cardinality metrics; SecurityEngineer must clear the redaction-allowlist extension. |

## Decision

**approved**

The observability architecture meets all six acceptance criteria for the
W4-A owned paths:

1. **Span-around-query**: One span per query execution via contextvar
   pairing, ambient parent context, graceful-degradation fallback. The
   zero-duration event-span anti-pattern is eliminated for queries.
2. **Low-cardinality metrics**: Labels bounded to enums; pool snapshots are
   metric values, not labels.
3. **Fingerprint opt-in**: Default OFF; `enable_exemplars()` opt-in only.
4. **Pool lifecycle**: Handlers + hook helpers are the correct contract for
   future W1-E dispatch sites. Absence of dispatch sites is a correctly
   scoped residual, not a W4-A defect.
5. **Prometheus rendering**: Text-exposition format with HELP/TYPE.
6. **W1-D Tier-A hook contract**: Original 10 keys preserved (additive
   only); `query_failure`/`hydration_failure`/`QueryTimer.__exit__`
   unchanged; `_redact` non-bypassable.

The two LOW findings are correctly-scoped follow-ups (transaction span
consistency and pool dispatch instrumentation), not blockers for W4-A's
owned-path architecture. The INFO finding (Tier-A key extension) is
architecturally sound and routed to the SecurityEngineer mandatory gate.

This record grants only the ChiefArchitect gate. It does not substitute for
the SecurityEngineer gate (mandatory — `errors_redaction: true`,
`_TIER_A_KEYS` extension) or the CodeReviewer gate (general code quality).
