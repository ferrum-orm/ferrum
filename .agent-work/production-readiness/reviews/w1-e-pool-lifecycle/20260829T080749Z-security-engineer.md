---
task_id: w1-e-pool-lifecycle
run_id: 20260829T080749Z
authority: SecurityEngineer
reviewer: security-engineer
reviewed_at: 2026-08-29T11:30:00Z
base_revision: 02d585513980ae89dbd6474196619a82faac460d
decision: approved
scope:
  - python/ferrum/drivers/postgres.py
  - python/ferrum/connection.py
  - tests/python/integration/test_connection.py
  - tests/python/integration/test_connection_runtime.py
---

# Named Authority Verdict

## Authority

SecurityEngineer

## Claims reviewed

Per AGENTS.md §3 (Security rules — credential handling, error boundaries)
and §5a (Safe error fields — pool exhaustion/failover must use structured
`category`), and the task contract
`.agent-work/production-readiness/tasks/w1-e-pool-lifecycle.md`:

- **S1 — DSN passwords never appear in diagnostics**: pool open failures,
  pool-not-open errors, drain-timeout errors, and any connection diagnostic
  surface carry only `host`, `port`, `database`, `username`, and `category`
  — never the password, full DSN, bound parameter values, PostgreSQL
  `DETAIL`/`HINT`, or row data.
- **S2 — Structured `category` on pool error paths**: every
  `FerrumConnectionError` / `FerrumTimeoutError` raised in
  `drivers/postgres.py` and `connection.py` passes `category=` from the
  W1-D error taxonomy (`config`, `connection`, `timeout`, `failover`,
  `pool_exhaustion`).
- **S3 — No bound parameter values, DETAIL, HINT, or row data** in pool
  error paths.
- **S4 — Connection diagnostics limited to allowlist**: `_redacted_diag`
  and `_redacted_dsn_info` return exactly `{host, port, database, username}`.
- **S5 — TLS/SSL configuration does not expose credentials**: `ssl`,
  `server_settings`, and `application_name` are passed to
  `asyncpg.create_pool()` as kwargs and never appear in any error message
  or diagnostic.

## Evidence

### Diff inspection

`git diff HEAD -- python/ferrum/drivers/postgres.py python/ferrum/connection.py
tests/python/integration/test_connection.py
tests/python/integration/test_connection_runtime.py` inspected end-to-end.

- **`_redacted_diag`** (`python/ferrum/drivers/postgres.py:125-147`):
  Returns exactly `{"host", "port", "database", "username"}` from
  `urlparse(dsn)`. Never reads `parsed.password`. Fallback on parse error
  returns `"unknown"` for all four fields. Docstring documents the §3
  credential allowlist.
- **`_redacted_dsn_info`** (`python/ferrum/connection.py:84-98`):
  Same allowlist, same fallback. Used by `Connection.open()` error path.
- **Pool open failure** (`postgres.py:297-304`, `connection.py:296-304`):
  Both raise `FerrumConnectionError` with `f"Failed to connect ... at
  {diag['host']}:{diag['port']} (database={diag['database']},
  username={diag['username']}): {type(exc).__name__} [FERR-E101]"` and
  `category="connection"`. Only the exception **class name** is included —
  never the asyncpg message, `DETAIL`, `HINT`, or row data.
- **Pool-not-open** (`postgres.py:353-361`, `connection.py:248-256`,
  `:381-387`, `:407-413`, `:493-499`, `:749-755`): All raise
  `FerrumConnectionError("Connection is not open. ... [FERR-E101]",
  category="config")`. No DSN, no secret.
- **Drain timeout** (`connection.py:328-334`): Raises
  `FerrumTimeoutError(f"Connection pool close timed out: {abandoned}
  in-flight operation(s) did not complete within {drain_timeout}s and
  may have been abandoned. [FERR-E102]", category="timeout")`. No DSN,
  no secret, no bound values.
- **Health check timeout** (`connection.py:350-354`): Raises
  `FerrumTimeoutError(..., category="timeout")`. No secret.
- **Transaction deadline** (`connection.py:530-535`): Raises
  `FerrumTimeoutError(..., category="timeout")`. No secret.
- **Failover handling** (`postgres.py:386-395`): `_handle_post_error`
  reads `getattr(mapped, "category", None)` and only calls
  `_expire_connections_safe()` when `category == "failover"`. The
  `failover` category is set by W1-D's `map_db_error` for
  `AdminShutdownError`/`CrashShutdownError`/`CannotConnectNowError`
  (SQLSTATE 57P01/57P02/57P03) at `errors.py:614-621`. No pre-ping, no
  diagnostic surface added.
- **`self._dsn` usage** (grep): only in `asyncpg.create_pool(self._dsn,
  **pool_kwargs)` (passed to asyncpg, not to messages),
  `_redacted_diag(self._dsn)` / `_redacted_dsn_info(self._dsn)` (allowlist
  applied), and `urlparse(self._dsn).scheme` (dialect detection only).
  Never logged, printed, or interpolated into an error message.
- **SSL/server_settings/application_name**: passed only as kwargs to
  `asyncpg.create_pool()` at `postgres.py:276-279`. Never appear in any
  `FerrumConnectionError`/`FerrumTimeoutError` constructor. The
  `application_name` is folded into `server_settings` via `setdefault`
  (`postgres.py:250-251`) before the pool is created — not stored in a
  way that could leak into an error path.
- **W1-D error taxonomy integration**: `map_db_error` (errors.py:606-641)
  sets `category="failover"` for admin/crash shutdown and
  `category="pool_exhaustion"` for `TooManyConnectionsError` (SQLSTATE
  53300). W1-E's `_handle_post_error` correctly reads this structured
  attribute. No string-matching on messages.

### Focused test run

```
FERRUM_TEST_DSN="postgresql://ferrum_test:ferrum_test@localhost:5432/ferrum_test"
FERRUM_TEST_REQUIRE_BACKENDS=postgres uv run pytest
tests/python/integration/test_connection.py
tests/python/integration/test_connection_runtime.py -x -v -m integration
-k "redact or dsn or leak or secret or password or credential or
failover or timeout or category"
```

- Exit: 0
- Output: `13 passed, 1 skipped, 20 deselected`
- The 1 skipped is MSSQL (pre-existing `aioodbc` import failure, not W1-E).
- Passing security-relevant tests:
  - `test_connection_failure_redacts_dsn[postgresql://...]` (pre-existing
    redaction test with the new config)
  - `test_dsn_redaction_with_ssl_config` (W1-E — bad DSN with
    `ssl="require"`, `server_settings`, `application_name`; asserts
    `"supersecret"` not in message and `"://"` not in message)
  - `test_new_config_does_not_leak_password_in_error` (W1-E — bad DSN with
    `command_timeout`, `statement_cache_size`, `ssl="prefer"`,
    `server_settings`; asserts `"mysecret"` and `"://"` not in message)
  - `test_failover_expire_on_shutdown_error` (W1-E — verifies
    `_handle_post_error` only expires on `category="failover"`, not on
    `category="connection"`)
  - `test_shutdown_reports_drain_timeout`,
    `test_shutdown_drain_timeout_reported` (W1-E — drain timeout carries
    `category="timeout"`, no secrets)
  - `test_acquire_timeout_enforced_on_fetch`,
    `test_acquire_timeout_enforced_on_execute`,
    `test_acquire_timeout_on_exhausted_pool` (W1-E — acquire timeout
    raises `FerrumTimeoutError`, no DSN leak)

### Full connection test suite

```
FERRUM_TEST_DSN=... uv run pytest
tests/python/integration/test_connection.py
tests/python/integration/test_connection_runtime.py -x -m integration
```

- Exit: 0
- Output: `33 passed, 1 skipped`

### Adversarial reproduction (fresh, not from executor)

`/tmp/w1e_security_check.py` — independent script that:

1. Opens `Connection` with a bad DSN containing
   `supersecret_password_12345_xyzzy_leak_probe` as the password, plus
   `ssl="require"`, `server_settings`, `application_name`,
   `command_timeout`, `statement_cache_size`.
   - `str(exc)`: `'Failed to connect to PostgreSQL at 127.0.0.1:59999
     (database=nodb, username=user): ConnectionRefusedError [FERR-E101]'`
   - `repr(exc)`: does not contain the secret.
   - `exc.category`: `"connection"`.
   - Secret not in `str(exc)`, `repr(exc)`, or case-insensitive `str(exc)`.
2. Pool-not-open error: `category="config"`, no secret.
3. Drain timeout error (constructed directly): `category="timeout"`, no
   secret, no DSN.
4. `_redacted_diag` and `_redacted_dsn_info` both return exactly
   `{'host': '127.0.0.1', 'port': '59999', 'database': 'nodb',
   'username': 'user'}` — no `password` key, no secret in any value, keys
   exactly match the §3 allowlist.

Result: `ALL SECURITY CHECKS PASSED`.

## Findings

### F1 — No finding: DSN redaction is structurally enforced

The `_redacted_diag` / `_redacted_dsn_info` helpers are the only source of
connection diagnostics in pool error paths. They return exactly the §3
allowlist (`host`, `port`, `database`, `username`). The raw `self._dsn` is
only passed to `asyncpg.create_pool()` (trusted library) and
`urlparse(...).scheme` (dialect detection). No code path interpolates the
raw DSN or password into an error message, log, or exception attribute.

### F2 — No finding: structured `category` on all pool error raises

Every `FerrumConnectionError(...)` and `FerrumTimeoutError(...)` raise in
the W1-E diff passes `category=`:

- `postgres.py`: `config` (pool not open), `connection` (open failure).
- `connection.py`: `config` (not open, x6 sites), `connection` (open
  failure), `timeout` (drain, health check, transaction deadline).

The `failover` and `pool_exhaustion` categories are sourced from W1-D's
`map_db_error` (`errors.py:614-621`, `:632-641`) as structured attributes,
not string-matched from messages. `_handle_post_error` reads
`getattr(mapped, "category", None)` — correct structured-field access.

### F3 — No finding: no DETAIL/HINT/row data in pool error paths

Pool open failures include only `type(exc).__name__` (the exception class
name). `map_db_error` (W1-D) sanitizes asyncpg exceptions to class name +
SQLSTATE only — `errors.py:653` documents "Sanitized: only the exception
class name; never DETAIL/HINT (ERR-1)". No pool error path in the W1-E
diff reads `DETAIL`, `HINT`, or row attributes from the driver exception.

### F4 — No finding: TLS/SSL config does not expose credentials

`ssl`, `server_settings`, and `application_name` are passed only as
kwargs to `asyncpg.create_pool()` (`postgres.py:276-279`). They are never
interpolated into error messages. `application_name` is folded into
`server_settings` before pool creation (`postgres.py:250-251`), so it
never lives on a separate attribute that could leak. The
`test_dsn_redaction_with_ssl_config` and
`test_new_config_does_not_leak_password_in_error` integration tests
explicitly pass SSL/server_settings config and assert the password does
not leak — both pass.

### F5 — Informational: `_redacted_diag` and `_redacted_dsn_info` are duplicated

Two near-identical allowlist helpers exist (`postgres.py:125` and
`connection.py:84`). This is a code-quality observation, not a security
defect — both correctly enforce the allowlist. The duplication predates
W1-E (W1-E only added the docstring to `_redacted_diag`). No correction
required for this gate; CodeReviewer may note it.

## Decision

`approved`

The W1-E pool lifecycle changes satisfy every §3 security rule and §5a
safe-error-field contract within the SecurityEngineer's scope:

- DSN passwords, full DSNs, bound parameter values, PostgreSQL
  `DETAIL`/`HINT`, and row data never appear in any pool error path,
  diagnostic, or exception attribute.
- Connection diagnostics are limited to the `{host, port, database,
  username}` allowlist.
- Every pool error raise carries a structured `category` from the W1-D
  error taxonomy.
- TLS/SSL, `server_settings`, and `application_name` configuration does
  not expose credentials in any error path.
- Failover handling reads the structured `category` attribute (not
  string matching) and only expires connections on `category="failover"`.

Fresh evidence: diff inspection, 13 security-filtered integration tests
passing, 33 full connection tests passing, and an independent adversarial
reproduction script confirming no secret leakage in `str(exc)`,
`repr(exc)`, or diagnostic dicts.

This record grants only the SecurityEngineer gate. It does not substitute
for ChiefArchitect (PoolStats/shutdown contract) or CodeReviewer (quality),
which remain pending.
