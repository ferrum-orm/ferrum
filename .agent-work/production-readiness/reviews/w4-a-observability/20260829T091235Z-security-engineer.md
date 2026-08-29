---
task_id: w4-a-observability
run_id: 20260829T091235Z
authority: SecurityEngineer
reviewer: security-engineer
reviewed_at: 2026-08-29T11:30:00Z
base_revision: b5e7ed3beaab60b7ded6ff6b1f8b77293ad376bb
decision: approved
scope:
  - python/ferrum/hooks.py (_TIER_A_KEYS extension + new helper functions)
  - python/ferrum/observability.py (metric labels, OTel span attrs, Prometheus render)
  - tests/python/unit/test_hooks_observability.py
  - tests/python/security/test_credential_safety.py
---

# Named Authority Verdict

## Authority

SecurityEngineer

## Claims reviewed

1. `_TIER_A_KEYS` extension (9 new keys) adds only safe enum/int/duration/boolean
   fields — no bound values, DSNs, credentials, or row data.
2. Default telemetry payloads never include bound values, DSNs, credentials, or
   row data.
3. Query fingerprints are NOT in default metric labels (opt-in exemplars only).
4. Tier B/C opt-in still requires Ferrum-specific opt-in (never from `DEBUG=1`).
5. W1-D redaction layer (`_redact()` / `_obs_level()` / Tier-A allowlist) is
   preserved and non-bypassable.

## Evidence

### Source inspection — `git diff HEAD -- python/ferrum/observability.py python/ferrum/hooks.py`

**`_TIER_A_KEYS` extension (hooks.py:43-70).** Original 10 W1-D keys preserved
(additive only): `event`, `model`, `table`, `operation`, `fingerprint`,
`duration_ms`, `status`, `failure_category`, `category`, `rows_affected`. Nine
new keys added after, each verified safe:

| Key | Type | Safety rationale |
|-----|------|------------------|
| `pool_size` | int | Integer pool snapshot count. Never a bound value or DSN. |
| `pool_idle` | int | Integer pool snapshot count. |
| `pool_acquired_count` | int | Integer pool snapshot count. |
| `pool_waiters` | int | Integer pool snapshot count. |
| `isolation` | enum str | Allowlisted isolation level (`serializable`/`repeatable_read`/`read_committed`/`read_uncommitted`/`default`). Not user free-form input. |
| `readonly` | bool | Boolean transaction flag. |
| `deferrable` | bool | Boolean transaction flag. |
| `direction` | enum str | Migration direction (`"up"` / `"down"`). Closed enum. |
| `attempt` | int | 1-based retry attempt number. Integer count. |

No new key carries a bound parameter value, DSN, credential, row datum, or
free-form user input. All are enums, integer counts, durations, or booleans
per §3 (tiered observability) and the task contract's `security_review_justification`.

**Redaction layer (hooks.py:86-107) — UNCHANGED by W4-A.** `_redact()` runs
before any hook receives data and keeps only `_TIER_A_KEYS` at Tier A. Tier B
adds `sql_normalized`; Tier C adds `sql_text` + `bound_params`. This function is
non-bypassable and was not modified by W4-A. The 9 new keys are automatically
covered by the existing allowlist filter since they are in `_TIER_A_KEYS`.

**Tier B/C opt-in (hooks.py:73-83) — UNCHANGED by W4-A.** `_obs_level()` reads
`FERRUM_OBS` (not `DEBUG`). Tier C additionally requires
`FERRUM_OBS_ALLOW_TIER_C=1`. `DEBUG=1` alone never elevates the tier. This
function was not modified by W4-A.

**Default metric labels (observability.py:112-128, `_safe_labels`).** Labels
restricted to a fixed tuple: `operation`, `status`, `category`,
`failure_category`, `isolation`, `direction`. `fingerprint` included only when
`include_fingerprint=True` (passed as `_EXEMPLARS_ENABLED`, default `False`).
No pool snapshot integers are used as labels — they are recorded as gauge
VALUES (`_on_pool_event`, observability.py:178-203), which is cardinality-safe.

**OTel span attributes (observability.py:336-356, `_span_attrs`).**
`span_attr_keys` frozenset is a subset of Tier-A-safe fields: `event`, `model`,
`table`, `operation`, `status`, `failure_category`, `category`, `rows_affected`,
`isolation`, `readonly`, `deferrable`, `direction`, `attempt`. `fingerprint`
added only when `_EXEMPLARS_ENABLED` is True (line 352-355). `duration_ms` added
separately as a float — safe (duration, not a bound value).

**New helper functions (hooks.py:307-499).** `pool_acquire`, `pool_release`,
`pool_wait`, `pool_timeout`, `pool_shutdown`, `transaction_start`,
`transaction_end`, `migration_event`, `retry_attempt`, `timeout_event` — all
dispatch payloads containing only Tier-A keys. None accept or carry `dsn`,
`password`, `bound_params`, `sql_text`, `row_data`, or free-form user input.
`migration_event` intentionally does NOT carry `migration_id` (cardinality +
free-form text). `retry_attempt` intentionally does NOT carry the exception
message. Verified via docstrings and dispatch payload construction.

**W1-D contract preservation.** `query_failure()` (hooks.py:212-240),
`hydration_failure()` (hooks.py:243-272), `QueryTimer.__exit__`
(hooks.py:288-306) — all UNCHANGED by W4-A (confirmed via diff: W4-A only
appends new code after `QueryTimer`). `query_start()`, `query_success()`
signatures and payloads UNCHANGED.

### Commands and output

```
uv run pytest tests/python/unit/test_hooks_observability.py tests/python/security/test_credential_safety.py -x -q
→ 20 passed, 25 deselected in 0.32s
```

The 25 deselected tests are from `test_credential_safety.py` (marked
`pytestmark = pytest.mark.security`); the default pytest config deselects
security-marked tests unless `-m security` is passed. The 20 passed tests are
from `test_hooks_observability.py` (no marker).

```
uv run pytest tests/python/security/test_credential_safety.py -x -q -m security
→ 25 passed in 0.19s
```

```
uv run pytest tests/python/security/ -x -q -m security
→ 144 passed in 0.39s
```

### Security-relevant test coverage verified

- `test_tier_a_keys_do_not_include_credentials` (test_credential_safety.py:135):
  asserts `_TIER_A_KEYS` has no overlap with `{dsn, password, credentials,
  secret, token, bound_params}`. **PASS.**
- `test_tier_a_keys_do_not_include_detail_or_hint` (test_credential_safety.py:404):
  asserts no `{detail, hint, bound_params, dsn, password, sql_text}` in
  `_TIER_A_KEYS`. **PASS.**
- `test_pool_helpers_never_carry_dsn_or_credentials`
  (test_hooks_observability.py:154): no `dsn`, `password`, or `://` in pool
  payload. **PASS.**
- `test_pool_helpers_strip_non_tier_a_keys` (test_hooks_observability.py:169):
  injected `dsn`, `password`, `bound_params` stripped by redaction. **PASS.**
- `test_migration_event_does_not_carry_migration_id`
  (test_hooks_observability.py:262): `migration_id` stripped (cardinality +
  free-form text). **PASS.**
- `test_retry_attempt_does_not_carry_exception_message`
  (test_hooks_observability.py:314): raw exception message stripped. **PASS.**
- `test_hook_payload_never_contains_dsn` (test_credential_safety.py:106):
  DSN/password injected into dispatch — stripped by redaction. **PASS.**
- `test_category_survives_tier_a_without_bound_values`
  (test_credential_safety.py:338): `bound_params` + `dsn` stripped, `category`
  retained. **PASS.**

## Findings

No security findings. All five claims reviewed pass with fresh evidence.

| # | Severity | Finding | Required correction |
|---|----------|---------|---------------------|
| — | — | None | — |

### Notes (non-blocking, no correction required)

1. **Residual risk (not a W4-A defect):** Pool lifecycle event dispatch sites
   in `connection.py`/`runtime.py` are not instrumented (W1-E follow-up). The
   handlers and helpers in W4-A owned paths are ready and safe. Pool metrics
   will not fire until dispatch sites are added. This does not affect
   telemetry payload redaction — the helpers are safe by construction and
   the redaction layer is non-bypassable regardless of dispatch source.

2. **Residual risk (not a W4-A defect):** `queryset.py` dispatches
   `query_failure` without `category=` (only `failure_category=`). This is a
   W1-A/W1-D integration gap outside W4-A owned paths. The observability
   handler correctly reads `category` when present and falls back to
   `failure_category` when absent. No security impact — `failure_category`
   is a Ferrum class name (Tier-A-safe metadata), not a bound value or
   secret.

3. **Cardinality note:** Pool snapshot integers (`pool_size`, `pool_idle`,
   `pool_acquired_count`, `pool_waiters`) are recorded as metric VALUES
   (gauges), NOT as metric labels. This is correct — using them as labels
   would be a cardinality risk. The `_safe_labels()` function correctly
   excludes them.

## Decision

**approved**

The `_TIER_A_KEYS` extension with 9 new keys is safe: every added key holds an
enum string, integer count, float duration, or boolean — never a bound value,
DSN, credential, or row datum. The W1-D redaction layer (`_redact()` /
`_obs_level()`) is preserved and non-bypassable. Default telemetry payloads
never include bound values, DSNs, credentials, or row data. Query fingerprints
are excluded from default metric labels and require explicit opt-in via
`enable_exemplars()`. Tier B/C opt-in remains Ferrum-specific (`FERRUM_OBS` /
`FERRUM_OBS_ALLOW_TIER_C`), never activated by `DEBUG=1`. All 144 security
tests and 20 hooks-observability unit tests pass.

This record grants only the SecurityEngineer gate for W4-A observability. It
does not substitute for the ChiefArchitect or CodeReviewer gates, which must
separately record `decision: approved`.
