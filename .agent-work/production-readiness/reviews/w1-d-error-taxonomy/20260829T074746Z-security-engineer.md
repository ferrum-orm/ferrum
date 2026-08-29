---
task_id: w1-d-error-taxonomy
run_id: 20260829T074746Z
authority: SecurityEngineer
reviewer: security-engineer-agent
reviewed_at: 2026-08-29T10:00:00Z
base_revision: 768ec1f3013f6d0eccd7c8b590ba36b54b12d23e
decision: approved
scope:
  - python/ferrum/errors.py redaction boundary (map_db_error / map_native_error)
  - python/ferrum/hooks.py Tier-A hook safety (_TIER_A_KEYS, query_failure, hydration_failure, QueryTimer)
  - tests/python/security/test_credential_safety.py DETAIL/HINT/DSN leak tests
  - Independent security test run
  - Adversarial probe with planted secrets
  - FerrumError attribute surface audit
  - Closed category enum membership
---

# Named Authority Verdict

## Authority

SecurityEngineer

## Claims reviewed

1. **C1 — Single redaction boundary**: `map_db_error` / `map_native_error` are the
   sole redaction seam. No other code path reads PostgreSQL `DETAIL`/`HINT` or
   bound parameter values and stores them on exception attributes.
2. **C2 — Sanctioned fields only**: The sanctioned safe-error-field set
   (`sqlstate`, `category`, `constraint`, `model`, `operation`) is the ONLY
   set of structured fields on mapped exceptions. No `detail`, `hint`,
   `message_detail`, `raw_message`, `raw_exception`, `dsn`, `password`,
   `bound_params`, `params`, `values`, or `row_data` attribute exists on
   `FerrumError` or any subclass.
3. **C3 — DETAIL/HINT/bound values/row data/DSNs never escape**: No mapped
   exception message, attribute, or default (Tier-A) hook payload contains
   PostgreSQL DETAIL, HINT, bound parameter values, row data, or full DSNs
   at any tier.
4. **C4 — Tier-A hook safety**: `_TIER_A_KEYS` includes `category` but
   excludes `bound_params`, `detail`, `hint`, `dsn`, `sql_text`, `password`.
   The `_redact` function is non-bypassable and runs before any hook
   receives data. Tier C (bound values) requires `FERRUM_OBS=C` AND
   `FERRUM_OBS_ALLOW_TIER_C=1`; a generic `DEBUG=1` never elevates the tier.
5. **C5 — Closed category enum**: Every `category` value produced by
   `map_db_error` is a member of `ERROR_CATEGORIES` (a `frozenset[str]`).
   No arbitrary user-supplied string can become a `category`.
6. **C6 — Security tests are deterministic and would fail on regression**:
   Tests plant sentinels in DETAIL/HINT/DSN and assert their absence from
   messages, attributes, and hook payloads.

## Evidence

### 1. Diff inspection — `python/ferrum/errors.py`

Inspected the full current source (828 lines) and the diff against base
revision `768ec1f3`.

- **`FerrumError` base class** (errors.py:110-147): declares exactly
  `code`, `sqlstate`, `category`, `constraint`, `model`, `operation`.
  `__init__` accepts `sqlstate`/`category`/`constraint`/`model`/`operation`
  as keyword-only args. No `detail`/`hint`/`raw_message`/`dsn`/`password`
  parameter or attribute exists.
- **`map_db_error`** (errors.py:477-767): every branch reads only
  `type(exc).__name__`, `getattr(exc, "sqlstate", None)`, and
  `getattr(exc, "constraint_name", None)` from the driver exception. No
  branch reads `.detail`, `.hint`, `.message`, or bound parameters. Messages
  are constructed from `type(exc).__name__` and the constraint name only.
- **`map_native_error`** (errors.py:770-828): passes `str(exc)` through for
  Rust-origin compile/hydration errors (documented as model/column names
  only, enforced at the Rust layer). Sets `category` from the closed enum.
  Never reads DETAIL/HINT/bound values.
- **`_extract_context`** (errors.py:463-474): extracts ONLY `model` and
  `operation` from the context dict. Docstring explicitly states the context
  dict MUST NOT contain bound parameter values or row data.
- **`_postgres_ddl_error_detail`** (errors.py:390-408): migration-path only.
  Reads `str(exc)` (the primary PostgreSQL MESSAGE field, not DETAIL/HINT)
  and `getattr(exc, "sqlstate", None)`. asyncpg's `PostgresError.__str__`
  returns the primary message; DETAIL and HINT are separate attributes
  (`.detail`, `.hint`) that are never read. This is consistent with the
  §5a contract, which prohibits DETAIL/HINT/bound values/row data/DSNs —
  not the primary DDL message (schema-level identifiers). Pre-existing
  behavior, not a W1-D introduction.
- **`migration_op_failure`** (errors.py:411-431): uses
  `_postgres_ddl_error_detail` + `_extract_sqlstate` + `_sqlstate_to_category`.
  No DETAIL/HINT/bound values threaded onto the `FerrumMigrationError`.

### 2. Diff inspection — `python/ferrum/hooks.py`

Inspected the full current source (287 lines) and the diff.

- **`_TIER_A_KEYS`** (hooks.py:38-51): `frozenset` containing exactly
  `event`, `model`, `table`, `operation`, `fingerprint`, `duration_ms`,
  `status`, `failure_category`, `category`, `rows_affected`. Does NOT
  include `bound_params`, `detail`, `hint`, `dsn`, `sql_text`, `password`,
  or any credential key.
- **`_redact`** (hooks.py:67-88): non-bypassable — called in `dispatch`
  (hooks.py:133) before any hook receives data. Tier A keeps only
  `_TIER_A_KEYS`. Tier B adds `sql_normalized` (no values). Tier C adds
  `sql_text` + `bound_params` — gated behind `FERRUM_OBS=C` AND
  `FERRUM_OBS_ALLOW_TIER_C=1` (hooks.py:62). `DEBUG=1` never elevates.
- **`query_failure`** (hooks.py:193-221): accepts `category: str | None`.
  Does NOT accept `bound_params`, `detail`, `hint`, `dsn`, or `sql_text`.
  When `category is None`, the key is omitted.
- **`hydration_failure`** (hooks.py:224-253): accepts `category: str | None`.
  Same safety properties as `query_failure`.
- **`QueryTimer.__exit__`** (hooks.py:269-287): extracts
  `category = getattr(exc_val, "category", None)` from FerrumError
  exceptions. Does NOT extract `bound_params`, `detail`, `hint`, `dsn`, or
  any other attribute. Only `category` is added to the payload (when truthy).

### 3. Diff inspection — `tests/python/security/test_credential_safety.py`

Inspected the full current source (408 lines) and the diff.

- **`TestDetailHintSafety`** (9 tests): plants
  `leaked_row_data_sentinel_42` in DETAIL and `hint_secret_password_99` in
  HINT across 9 exception types (UniqueViolation, Deadlock, Serialization,
  ConnectionFailure, UndefinedColumn, PostgresError, AdminShutdown,
  CrashShutdown, CannotConnectNow, TooManyConnections). Asserts sentinels
  absent from `str(result)` and from all structured attributes
  (`sqlstate`, `category`, `constraint`, `model`, `operation`).
  `test_dsn_in_timeout_message_does_not_leak` plants a full DSN in a
  `TimeoutError` and asserts the password and DSN are absent from the
  mapped error message.
- **`TestCategoryAndHookSafety`** (4 tests):
  `test_category_survives_tier_a_without_bound_values` dispatches a payload
  with `bound_params`, `dsn`, and `category` keys; asserts `bound_params`
  and `dsn` are stripped, `category` survives, and `supersecret` does not
  appear in the payload string.
  `test_all_mapped_categories_are_in_closed_enum` maps 17 asyncpg exception
  types and asserts every resulting `category` is in `ERROR_CATEGORIES`.
  `test_tier_a_keys_include_category` and
  `test_tier_a_keys_do_not_include_detail_or_hint` assert the Tier-A
  allowlist composition.
- Tests are deterministic: they use `mock.MagicMock(spec=...)` with planted
  sentinels and would fail immediately if the redaction boundary were
  broken (the mapped message would contain the sentinel).

### 4. Independent security test run

```
$ uv run pytest tests/python/security/test_credential_safety.py -x -q -s -m security
25 passed in 0.18s
```

All 25 security tests pass. No failures, no warnings, no errors.

### 5. Adversarial probe (independent, not added to repo)

Wrote and executed a temporary script that:

1. Created `mock.MagicMock(spec=asyncpg.exceptions.UniqueViolationError)`
   with `detail` containing `SECRET_ROW_VALUE_xyz789` and `hint` containing
   `SECRET_HINT_password_abc123`. Mapped via `map_db_error`. Checked
   `str(result)`, all structured attributes (`sqlstate`, `category`,
   `constraint`, `model`, `operation`, `field`, `operator`, `code`), and
   all `__dict__` values for the secrets. **All absent.**
2. Repeated for generic `PostgresError` (SQLSTATE `XX000`). **All absent.**
3. Planted a full DSN (`postgresql://admin:hunter2@...`) in a `TimeoutError`
   message. Mapped via `map_db_error`. Asserted DSN and password absent
   from the mapped message. **Pass.**
4. Dispatched a Tier-A hook payload containing `bound_params`,
   `dsn`, `detail`, `hint`, `sql_text`, `password`, and `category` keys.
   Verified the received payload contains `category` but none of
   `bound_params`/`dsn`/`detail`/`hint`/`sql_text`/`password`, the payload
   keys are a subset of `_TIER_A_KEYS`, and no secret string appears in
   `str(payload)`. **Pass.**
5. Verified `FerrumError` has no unsafe attributes (`detail`, `hint`,
   `message_detail`, `raw_message`, `raw_exception`, `dsn`, `password`,
   `bound_params`, `params`, `values`, `row_data`). **None exist.**
6. Mapped all 17 asyncpg exception types with planted DETAIL/HINT secrets
   and verified every `category` is in `ERROR_CATEGORIES` and no secret
   appears in any mapped message. **All pass.**

Result: **ALL ADVERSARIAL PROBES PASSED** (88 individual assertions).
Script deleted after running; not added to the repository.

### 6. Codebase-wide grep for DETAIL/HINT reads

```
$ grep -r '\.(detail|hint)\b' python/ferrum/ --include='*.py'
No files found
```

No code path in `python/ferrum/` reads `.detail` or `.hint` from any
exception object. The redaction boundary is structural, not string-based.

### 7. Codebase-wide grep for bound_params on exceptions

`bound_params` appears in `queryset.py` (query execution), `echo.py`
(opt-in verbose SQL logging), `hooks.py` (Tier C only, gated), and
`drivers/protocol.py` (`CompiledQuery` dataclass). None of these store
`bound_params` on any exception attribute. The hooks Tier-C path is
gated behind `FERRUM_OBS=C` + `FERRUM_OBS_ALLOW_TIER_C=1` and never
activates from `DEBUG=1`.

## Findings

**None.** No security defect, missing redaction, leaked secret, or
missing test was found in the W1-D implementation.

### Notes (not defects)

- **`_postgres_ddl_error_detail` reads `str(exc)`**: This is the migration
  DDL path only. `str(exc)` on an asyncpg `PostgresError` returns the
  primary MESSAGE field, not DETAIL/HINT (which are separate `.detail`/
  `.hint` attributes that are never read). The primary DDL message is
  schema-level (table/column/constraint names). The §5a contract
  prohibits DETAIL/HINT/bound values/row data/DSNs — not the primary
  message. This is pre-existing behavior, not a W1-D introduction, and
  does not violate the contract.
- **`queryset.py` call sites do not yet pass `category` to
  `query_failure()`**: Noted by the executor as a follow-up for another
  workstream. The hooks infrastructure is ready; `category` is threaded
  when callers pass it. This is not a security defect — `category` is a
  closed-enum string, and its absence does not weaken the redaction
  boundary.
- **`observability.py` OTel hook has a hardcoded key list without
  `category`**: Noted by the executor as a follow-up. Not a security
  defect — `category` is a safe Tier-A key; the OTel hook simply does
  not emit it yet.

## Decision

**approved**

The W1-D error taxonomy and diagnostics implementation satisfies every
§3 Security rule and every §5a "Safe error fields" contract requirement
within the errors/redaction surface:

- `map_db_error` / `map_native_error` are the single, structural
  redaction boundary. No code path reads DETAIL, HINT, bound parameter
  values, row data, or full DSNs and stores them on any exception
  attribute.
- The sanctioned safe-error-field set (`sqlstate`, `category`,
  `constraint`, `model`, `operation`) is the ONLY set of structured
  fields on mapped exceptions. No unsafe attribute exists.
- Tier-A hook payloads include `category` (closed enum) and exclude
  `bound_params`/`detail`/`hint`/`dsn`/`sql_text`/`password`. The
  redaction is non-bypassable. Tier C is double-gated and never
  activates from `DEBUG=1`.
- Security tests are deterministic, plant real secrets in DETAIL/HINT/DSN,
  and prove the secrets never escape. All 25 tests pass.
- An independent adversarial probe with 88 assertions confirmed the
  redaction boundary holds across 17 exception types, the hook payload
  path, and the FerrumError attribute surface.

This record grants only the SecurityEngineer's gate for the
errors/redaction surface. It does not substitute for the ChiefArchitect
(closed category enum matches §5a) or CodeReviewer (code-quality) gates,
which are required separately for the `complete` transition.
