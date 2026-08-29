---
task_id: w1-d-error-taxonomy
run_id: 20260829T074746Z
authority: ChiefArchitect
reviewer: chief-architect-agent
reviewed_at: 2026-08-29T07:57:28Z
base_revision: 768ec1f3013f6d0eccd7c8b590ba36b54b12d23e
decision: approved
scope:
  - Closed category enum (ERROR_CATEGORIES) matches ratified §5a "Safe error fields" contract
  - Exception class hierarchy is architecturally sound
  - PyO3 boundary contract (§4) preserved
  - Tier-A hook contract (§3) preserved
  - No new components or failure modes beyond task contract scope
---

# Named Authority Verdict

## Authority

ChiefArchitect

## Claims reviewed

1. **Closed category enum**: `ERROR_CATEGORIES` is a closed `frozenset[str]`
   whose values are stable string literals (not dynamically generated), covering
   all §5a-required PostgreSQL/driver classes and Ferrum-level categories, with
   no extra FIELDS beyond the sanctioned set
   (`sqlstate`, `category`, `constraint`, `model`/`operation`).

2. **Exception class hierarchy**: The existing Ferrum public exception types
   are preserved (no renames, no restructured hierarchy);
   `map_db_error` / `map_native_error` remain the single redaction boundary;
   every mapped exception class carries `sqlstate` and `category` as structured
   attributes; the chaining policy is safe.

3. **Architectural soundness**: The PyO3 boundary contract (§4) is preserved
   (Rust panic mapping stays catchable, no Rust changes); no new components or
   failure modes are introduced beyond the task contract; the Tier-A hook
   contract (§3) is preserved (`category` added, no bound values leak).

## Evidence

### Inspected paths

- `python/ferrum/errors.py` — full diff and source (lines 68-828)
- `python/ferrum/hooks.py` — full diff and source (lines 1-287)
- `AGENTS.md` §2 (non-negotiable constraints), §3 (security rules), §4 (PyO3
  boundary), §5a (Wave 0 contracts — "Safe error fields" subsection)
- `.agent-work/production-readiness/tasks/w1-d-error-taxonomy.md`
- `.agent-work/production-readiness/logs/w1-d-error-taxonomy/20260829T074746Z.md`
- `.agent-work/production-readiness/verification/w1-d-error-taxonomy/20260829T074746Z.md`

### Commands run

- `git diff HEAD -- python/ferrum/errors.py python/ferrum/hooks.py` — inspected
  the full 416+/53- diff (416 insertions, 53 deletions across both files).
- `git rev-parse HEAD` → `768ec1f3013f6d0eccd7c8b590ba36b54b12d23e` (matches
  base_revision).
- `grep -n "raise.*from\|__cause__\|__context__\|__suppress_context__"
  python/ferrum/errors.py python/ferrum/hooks.py` → no output (no exception
  chaining in mapping functions; they return new exceptions, never chain
  originals).

### Source citations

**`ERROR_CATEGORIES` (errors.py:68-146)**: Declared as
`ERROR_CATEGORIES: frozenset[str] = frozenset({...})` with 29 static string
literals. Covers: `integrity`, `integrity_error`, `unique_violation`,
`foreign_key_violation`, `not_null_violation`, `check_violation`, `schema`,
`undefined_function`, `undefined_column`, `undefined_table`, `serialization`,
`deadlock`, `lock_timeout`, `query_cancellation`,
`invalid_transaction_state`, `failover`, `connection`, `timeout`,
`pool_exhaustion`, `compile_error`, `hydration`, `internal`, `config`,
`not_found`, `multiple_objects`, `deferred_field`, `relation_not_loaded`,
`danger_api`, `migration`, `unknown`. All required §5a classes are present.
No values are dynamically generated; all are literal strings.

**`FerrumError` base class (errors.py:148-194)**: Declares class attributes
`sqlstate: str | None = None`, `category: str | None = None`,
`constraint: str | None = None`, `model: str | None = None`,
`operation: str | None = None`. `__init__` accepts them as keyword-only
args. No fields beyond the sanctioned set plus pre-existing `code`
(FERR-XXXX error code, not a DB-derived safe field).

**Exception hierarchy preserved**: `FerrumConfigError`, `FerrumCompileError`,
`FerrumDeferredFieldError`, `FerrumRelationNotLoadedError`,
`FerrumNotFoundError`, `FerrumMultipleObjectsError`, `FerrumIntegrityError`,
`FerrumConnectionError`, `FerrumTimeoutError`, `FerrumInternalError`,
`FerrumHydrationError`, `FerrumMigrationError`, `FerrumDangerApiError`,
`FerrumSchemaError`, `FerrumDatabaseError` — all present, no renames, no
hierarchy restructuring. `FerrumCompileError` and `FerrumIntegrityError`
override `__init__` and thread sanctioned fields via `super().__init__()`;
all others inherit `FerrumError.__init__` directly.

**Class-level category defaults**: `FerrumConfigError` → `"config"`,
`FerrumDeferredFieldError` → `"deferred_field"`,
`FerrumRelationNotLoadedError` → `"relation_not_loaded"`,
`FerrumNotFoundError` → `"not_found"`,
`FerrumMultipleObjectsError` → `"multiple_objects"`,
`FerrumInternalError` → `"internal"`, `FerrumHydrationError` → `"hydration"`,
`FerrumMigrationError` → `"migration"`, `FerrumDangerApiError` →
`"danger_api"`. All values are in `ERROR_CATEGORIES`.

**`map_db_error` (errors.py:477-779)**: Every branch sets `category` (from
the closed enum) and `sqlstate` (where applicable — PostgreSQL branches via
`getattr(exc, "sqlstate", None)`; non-PG branches leave `sqlstate=None`).
`model`/`operation` threaded via `_extract_context` which only reads
`{"model", "operation"}` keys (errors.py:463-474) — allowlist extraction,
never reads `bound_params`, `detail`, `hint`, `dsn`, or row data.

New asyncpg mappings: `DeadlockDetectedError` (40P01→deadlock),
`SerializationError` (40001→serialization),
`LockNotAvailableError` (55P03→lock_timeout), `AdminShutdownError`/
`CrashShutdownError`/`CannotConnectNowError` (57P01/57P02/57P03→failover),
`InvalidTransactionStateError` (25xxx→invalid_transaction_state),
`TooManyConnectionsError` (53300→pool_exhaustion),
`UndefinedFunctionError` (42883→undefined_function),
`QueryCanceledError` (57014→query_cancellation). Generic `PostgresError`
fallback uses `_sqlstate_to_category(sqlstate)`.

**`map_native_error` (errors.py:781-828)**: Sets `category` on
`FerrumCompileError` ("compile_error"), `FerrumHydrationError` ("hydration"),
`FerrumInternalError` ("internal"), and the `RuntimeError` fallback
("internal"). `sqlstate` is `None` for all native (non-database) exceptions.
No Rust code changes (confirmed: `git diff HEAD -- crates/ferrum-pyo3/`
returns empty).

**`_TIER_A_KEYS` (hooks.py:38-51)**: `frozenset` containing `event`,
`model`, `table`, `operation`, `fingerprint`, `duration_ms`, `status`,
`failure_category`, `category`, `rows_affected`. Does NOT include `detail`,
`hint`, `bound_params`, `dsn`, `password`, or `sql_text`. The `category`
value is a closed-enum string, never user input.

**`QueryTimer.__exit__` (hooks.py:270-287)**: Extracts
`category = getattr(exc_val, "category", None)` and adds it to payload only
when truthy. Safe — `category` is the closed-enum string from `FerrumError`.

**Chaining policy**: `grep` for `raise.*from`, `__cause__`, `__context__`,
`__suppress_context__` in errors.py and hooks.py returned no output. The
mapping functions return new `FerrumError` instances; they do not raise or
chain the original exception. The original driver exception is not stored
as `__cause__` or `__context__` by the mapping layer.

## Findings

### No defects found

The implementation satisfies every claim within the ChiefArchitect scope.

### Minor in-scope observations (not defects — no correction required)

1. **Reserved enum values**: Five categories (`foreign_key_violation`,
   `not_null_violation`, `check_violation`, `undefined_column`,
   `undefined_table`) are in `ERROR_CATEGORIES` but not currently produced
   by any `map_db_error` branch. The broad `IntegrityError` catch maps to
   `integrity_error`, and the broad schema catch maps to `schema`. Reserved
   values in a closed enum are architecturally sound — they stabilize the
   contract for future refinement without reopening the enum. The task
   contract does not require subcategorizing integrity violations beyond
   unique.

2. **`map_native_error` does not thread `model`/`operation`**: The
   `FerrumCompileError` branch in `map_native_error` passes `str(exc)` and
   `category="compile_error"` but not `model`/`operation` (not available at
   that layer). The fields default to `None`. This is consistent with §5a —
   the structured attributes exist on every exception; their values are
   `None` when the mapping layer lacks the context. The caller (e.g.,
   `queryset.py`) is a separate workstream's owned path.

3. **Non-PostgreSQL branches set `category` but not `sqlstate`**:
   `sqlstate` is a PostgreSQL-specific SQLSTATE code. Non-PG branches
   (asyncmy, aiosqlite, pyodbc) leave `sqlstate=None`. This is correct —
   §5a requires `sqlstate` as a structured attribute on every mapped
   exception (which it is, via the base class), not a non-`None` value on
   every exception.

4. **Pre-existing `field`/`operator` on `FerrumCompileError`**: These
   attributes predate W1-D and are Ferrum-side compile metadata (model field
   name and query operator from metadata allowlists), not DB-derived safe
   fields. They are analogous in nature to `model`/`operation` and do not
   carry DETAIL/HINT/bound values/row data/DSNs. Not a §5a violation.

## Decision

**approved**

The closed category enum (`ERROR_CATEGORIES`) matches the ratified §5a
"Safe error fields" contract: it is a `frozenset[str]` of stable string
literals covering all required PostgreSQL/driver classes, with no extra
FIELDS beyond the sanctioned set (`sqlstate`, `category`, `constraint`,
`model`/`operation`). The exception class hierarchy is architecturally
sound: all public types are preserved, `map_db_error`/`map_native_error`
remain the single redaction boundary, every mapped class carries
`sqlstate`/`category` as structured attributes, and the chaining policy is
safe. The PyO3 boundary contract (§4) and Tier-A hook contract (§3) are
preserved. No new components or failure modes are introduced beyond the
task contract scope.

This record grants only the ChiefArchitect gate. It does not substitute
for the SecurityEngineer or CodeReviewer gates, or for independent
verification.
