---
task_id: w2-d-framework-integrations
run_id: 20260829T091235Z
authority: SecurityEngineer
reviewer: security-engineer
reviewed_at: 2026-08-29T12:30:00Z
base_revision: b5e7ed3beaab60b7ded6ff6b1f8b77293ad376bb
decision: approved
scope:
  - auth/credential handling in FastAPI dependency injection
  - error translation (no DSN/bound-value/row-data leakage in HTTP responses)
  - fastapi-users adapter does not expose raw connections
---

# Named Authority Verdict

## Authority

`SecurityEngineer` — auth/credential handling, error redaction, FastAPI
dependency injection and error translation security surface.

## Claims reviewed

Per the task contract `security_surfaces.auth_secrets: true` and
`security_review: required`, and `AGENTS.md` §3 (Security rules):

- **S1**: Error translation (`map_ferrum_to_http_status` +
  `ferrum_exception_handler` + `_sanitize_ferrum_error_payload`) emits only
  the sanctioned safe-error-field set (`code`/`category`/`sqlstate`/
  `constraint`/`model`/`operation` per ratified §5a). DSNs, bound parameter
  values, DETAIL/HINT, and row data never appear in HTTP response bodies.
- **S2**: `FerrumUserDatabase` does not store, construct, or expose raw
  connections, pools, drivers, or DSNs. The caller controls the connection
  per-call.
- **S3**: No credential leakage in any HTTP response path (error handler
  and dependency injection).
- **S4**: `str(exc)` is never read by the error handler — defensive
  boundary against sanitized-but-untrusted message text.
- **S5**: No logging of credentials, DSNs, or secrets in the contrib module.

## Evidence

### Source inspection — `git diff HEAD -- python/ferrum/contrib/fastapi.py`

Inspected the full diff (505 insertions). Key security-relevant findings:

**`_sanitize_ferrum_error_payload`** (diff lines ~290-310): builds a
`dict[str, Any]` with exactly `code` (always), then conditionally
`category`/`sqlstate`/`constraint`/`model`/`operation` (only when not
`None`). The function never calls `str(exc)`, never reads `__dict__`,
never accesses `args` or `__cause__`. This is the sole payload builder for
HTTP responses. Matches the ratified §5a safe-error-field set exactly.

**`ferrum_exception_handler`** (diff lines ~315-325): calls
`_sanitize_ferrum_error_payload(exc)` and returns
`starlette.responses.JSONResponse(status_code=..., content={"error": payload})`.
The response body is exactly `{"error": <safe payload>}`. No other data is
attached. `str(exc)` is never referenced.

**`FerrumUserDatabase.__init__`** (diff lines ~425-432): accepts
`user_model`, `oauth_account_model`, `user_fk_field` only. No `conn`,
`dsn`, `database_url`, `password`, or credential parameter. Stores only
`self._base_protocol`, `self.user_model`, `self.oauth_account_model`,
`self.user_fk_field`. No connection/pool/driver/DSN attribute.

**`FerrumUserDatabase` CRUD methods** (diff lines ~434-560): each method
takes `conn: Connection | Transaction` as an explicit first parameter
(after `self`). The connection is passed through to QuerySet terminals
(`objects.get`, `objects.filter`, `objects.create`,
`objects.update_instance`, `objects.filter().delete`). The adapter never
constructs a connection, never accesses `conn._pool`, `conn._driver`,
`conn._require_driver()`, or any pool/driver internal. No credential or
DSN is read from the connection.

**`get_ferrum_conn` / `get_ferrum_transaction`** (diff lines ~218-260):
access `request.app.state.ferrum_conn` via `getattr`. If not a
`Connection`, raise `RuntimeError` with a fixed string:
`"Ferrum connection is not initialized. In the app lifespan, open
ferrum_lifespan and set app.state.ferrum_conn = conn."` — no DSN, no
credentials, no user data. This `RuntimeError` is not a `FerrumError` and
is not caught by `ferrum_exception_handler` (which registers only for
`FerrumError`); FastAPI's default 500 handler surfaces it generically.

**`ferrum_lifespan`** (diff lines ~70-175): passes `database_url` to
`ferrum.connect()`. The DSN is forwarded, never logged, never stored on
`app.state`. The docstring explicitly documents "The DSN is never logged
(CRED-1)". The `echo` parameter threads to `ferrum.connect()` (W1-E
domain — Tier B/C opt-in SQL logging, not credential logging).

**`update_oauth_account`** (diff lines ~515-530): raises
`FerrumNotFoundError` with a message containing `user.id` and `oauth_name`
(e.g., `"OAuth account for provider 'google' not found on user 'usr_123'.
[FERR-Q404]"`). This message goes into `str(exc)`, but
`_sanitize_ferrum_error_payload` never reads `str(exc)`, so neither
`user.id` nor `oauth_name` appears in the HTTP response body. The
message contains no DSN, no bound parameter values, no DETAIL/HINT, no
row data. The redaction layer for exception messages is W1-D's domain
(already complete); within W2-D, the handler is a defensive boundary
that keeps the response shape minimal.

**`_raise_user_already_exists`** (diff lines ~560-570): chains the
original `FerrumIntegrityError` as `__cause__` via
`raise UserAlreadyExists() from exc`. The `FerrumIntegrityError` message
is sanitized by W1-D's redaction layer (no DETAIL/HINT/row data per §3
and §5a). The `__cause__` chain is a Python exception chain, not an HTTP
response body. When `fastapi-users` is not installed, the original
`FerrumIntegrityError` is re-raised and goes through
`ferrum_exception_handler` with safe fields only.

**No logging** — `grep -n "logging\|logger\|log\.\|print("` returns no
matches in the module. No credential, DSN, or secret is logged anywhere
in `contrib/fastapi.py`.

### Adversarial probe — `ferrum_exception_handler` with crafted exceptions

Ran 6 independent adversarial probes directly against
`ferrum_exception_handler` with exceptions whose `str(exc)` contains DSN,
DETAIL, HINT, email, password, and row data:

```
$ uv run python -c "..."  (see review evidence below)

Probe 1: FerrumIntegrityError with DSN + DETAIL + email + password + row data
  → 409 body: {'error': {'code': 'FERR-D201', 'category': 'unique_violation',
    'sqlstate': '23505', 'constraint': 'uq_users_email', 'model': 'User',
    'operation': 'insert'}}
  → Asserted 'user@example.com', 'secretpw', '10.0.0.1', 'prod', 'DETAIL',
    'HINT', 'hashed_pw', 'Duplicate key', 'postgresql://' all ABSENT. PASS.

Probe 2: FerrumConnectionError with full DSN in str(exc)
  → 503 body: {'error': {'code': 'FERR-E101', 'category': 'connection',
    'sqlstate': '08001'}}
  → Asserted 'adminpw', 'db.internal', 'admin', 'DSN=', 'postgresql://' all
    ABSENT. PASS.

Probe 3: FerrumDatabaseError with row data in str(exc)
  → 500 body: {'error': {'code': 'FERR-D001', 'category': 'undefined_table',
    'sqlstate': '42P01'}}
  → Asserted 'alice@corp.com', '99999', 'salary' all ABSENT. PASS.

Probe 4: base FerrumError with arbitrary secret in str(exc)
  → 500 body: {'error': {'code': 'FERR-0000'}}
  → Asserted 'abc123', 'arbitrary message' ABSENT. PASS.

Probe 5: FerrumNotFoundError with user.id + oauth_name in str(exc)
  (simulates update_oauth_account pattern)
  → 404 body: {'error': {'code': 'FERR-Q404', 'category': 'not_found'}}
  → Asserted 'google', 'usr_123', 'OAuth account' ABSENT. PASS.

Probe 6: payload key set is subset of sanctioned set
  → Asserted actual_keys ⊆ {'code','category','sqlstate','constraint',
    'model','operation'}. PASS.

ALL ADVERSARIAL PROBES PASS
```

### `FerrumUserDatabase` connection exposure probe

```
$ uv run python -c "..."  (inspect.getsource + signature inspection)

- No `self.conn`, `self._conn`, `self.pool`, `self._pool`, `self.driver`,
  `self._driver`, `self.dsn`, `self._dsn`, `self.database_url`,
  `self._database_url` in source. PASS.
- __init__ params: ['self', 'user_model', 'oauth_account_model',
  'user_fk_field'] — no conn/dsn/database_url. PASS.
- All 9 CRUD methods take `conn` as explicit per-call first parameter. PASS.
```

The adapter does not store, construct, or expose raw connections, pools,
drivers, or DSNs. The caller controls the connection per-call.

### Unit tests — fresh run

```
$ uv run pytest tests/python/unit/test_contrib_fastapi.py -x -q
→ 27 passed, 4 skipped in 0.71s
```

4 skipped = `fastapi_users` not installed (soft-import tests).

Security-relevant tests confirmed in `test_contrib_fastapi.py`:
- `test_ferrum_exception_handler_returns_json_response_with_safe_fields`
  (line 246) — asserts payload has exactly `code`/`category`/`sqlstate`/
  `constraint`/`model`/`operation`.
- `test_ferrum_exception_handler_does_not_leak_message_or_dsn` (line 268)
  — crafts an exception with `DETAIL (key)=(user@example.com)` and
  `DSN=postgresql://user:password@host:5432/db` in `str(exc)`, asserts
  `user@example.com`, `password`, `host`, `DETAIL`, `Duplicate key` all
  absent from the response body.
- `test_ferrum_exception_handler_omits_none_fields` (line 294) — asserts
  None safe fields are omitted (not emitted as `null`).
- `test_register_ferrum_exception_handlers_roundtrip_via_asgi` (line 320)
  — end-to-end ASGI roundtrip confirming 409/503 status codes and safe
  payload.

### Import boundary (security-relevant)

The eager `from starlette.requests import Request as _StarletteRequest`
is confined to `ferrum.contrib.fastapi` (not `ferrum.contrib.__init__`).
Core Ferrum never imports `fastapi`/`starlette`/`fastapi_users` —
confirmed by the verifier's subprocess clean-room test and
`.importlinter` `cli-isolation` / `contrib-isolation` contracts (0
broken). This is not a credential-handling concern but confirms the
security boundary is structural.

## Findings

| # | Severity | Evidence | Required correction |
|---|---|---|---|
| — | None | All probes pass, source inspection confirms no credential/DSN/bound-value/row-data leakage in any HTTP response path. `FerrumUserDatabase` does not store or expose raw connections. No logging of credentials. | None. |

### Non-blocking observations

- `update_oauth_account` raises `FerrumNotFoundError` with `user.id` and
  `oauth_name` in the message string. The handler never includes
  `str(exc)` in the response, so these do not leak. The message is a
  fixed-format string with no DSN or bound parameter values. The
  redaction layer for exception messages is W1-D's domain (already
  complete). No action required within W2-D.
- `_raise_user_already_exists` chains the original `FerrumIntegrityError`
  as `__cause__`. The `FerrumIntegrityError` message is sanitized by
  W1-D's redaction layer. The `__cause__` chain is a Python exception
  chain, not an HTTP response body. No action required within W2-D.
- `get_ferrum_conn` / `get_ferrum_transaction` raise `RuntimeError` (not
  `FerrumError`) when the connection is uninitialized. The message is a
  fixed string with no secrets. This is not caught by
  `ferrum_exception_handler`; FastAPI's default 500 handler surfaces it
  generically. No action required.

## Decision

**`approved`**

The FastAPI dependency injection and error translation satisfy all
`AGENTS.md` §3 security rules for auth/credential handling:

1. **SQL safety** — N/A (no SQL compilation in this surface).
2. **Credential handling** — DSNs, passwords, and secrets never appear
   in HTTP response bodies, exception messages echoed to clients, hook
   payloads, or logs. The `ferrum_lifespan` forwards the DSN to
   `ferrum.connect()` without logging it (CRED-1, documented in the
   docstring). `FerrumUserDatabase` does not accept or store a DSN.
3. **Tiered observability** — the contrib module performs no logging.
   The `echo` parameter threads to `ferrum.connect()` (W1-E domain; Tier
   B/C opt-in, never activates from `DEBUG=1`).
4. **Error boundaries** — `ferrum_exception_handler` emits only the
   sanctioned safe-error-field set (`code`/`category`/`sqlstate`/
   `constraint`/`model`/`operation`). `str(exc)` is never read. Six
   adversarial probes with crafted exceptions carrying DSN, DETAIL,
   email, password, and row data confirm zero leakage.
5. **`FerrumUserDatabase`** — does not store, construct, or expose raw
   connections, pools, drivers, or DSNs. The caller controls the
   connection per-call. OAuth account fields (`access_token`,
   `refresh_token`) are passed as bound parameters to QuerySet terminals
   — never logged, never in error messages, never in HTTP responses.

This record grants only the SecurityEngineer gate for W2-D. It does not
substitute for the ChiefArchitect or CodeReviewer gates, or for
independent verification.
