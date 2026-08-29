---
task_id: w2-f-schema-drift-cli
run_id: 20260829T091235Z
authority: ChiefArchitect
reviewer: ChiefArchitect
reviewed_at: 2026-08-29T14:10:00Z
base_revision: b5e7ed3beaab60b7ded6ff6b1f8b77293ad376bb
decision: approved
scope:
  - python/ferrum/migrations/drift.py
  - python/ferrum/cli/check_schema_cmd.py
  - python/ferrum/cli/app.py
---

# Named Authority Verdict

## Authority

ChiefArchitect

## Claims reviewed

1. **Drift detection architecture**: comprehensive comparison (columns, types,
   nullability, defaults, PK, constraints, indexes, extensions, RLS policies,
   functions, vector dimensions) with read-only introspection.
2. **JSON output for CI**: machine-readable `--json` with stable, complete keys.
3. **Unmanaged exclusions**: Better Auth, LangGraph, Alembic-owned objects
   excluded by default with override capability.
4. **Alembic coexistence design**: Alembic remains authoritative; Ferrum only
   reads and reports drift, never applies DDL.
5. **Read-only introspection**: no DDL, no credential surfaces, no identifier
   interpolation; respects §2.9 (allowlisted identifiers, bound parameters) and
   the W1-C boundary (no `orchestrator.py`/`ledger.py`/`operations.py` edits).
6. **Additive dataclass evolution**: existing `format_summary()` callers keep
   working; new difference kinds are additive fields/dict keys.

## Evidence

### Architecture inspection

- `python/ferrum/migrations/drift.py` (377 → ~1500 lines, additive):
  - New frozen dataclasses `IndexDifference`, `ConstraintDifference`,
    `ExtensionDifference`, `PolicyDifference`, `FunctionDifference`, each with
    `to_dict()`. `DriftReport.to_dict()` / `to_json()` emit 17 top-level keys
    with `sort_keys=True` for stable CI diffs.
  - `_fetch_postgres_schema` returns a 7-tuple: columns, primary keys, indexes,
    constraints, extensions, policies, functions. Every schema-scoped query
    uses `$1` for the schema name (column, PK, index, constraint, policy,
    function queries). The extension query is catalog-wide (no parameter,
    no user input). No identifier interpolation anywhere. All queries are
    `SELECT`; no `CREATE`/`ALTER`/`DROP`/`INSERT`/`UPDATE` — confirmed by
    reading each query string in the diff.
  - `ferrum_migrations` hardcoded in `_SYSTEM_TABLES`; always excluded.
    Default unmanaged presets (`_DEFAULT_AUTH_TABLES`,
    `_DEFAULT_LANGGRAPH_TABLES`, `_DEFAULT_ALEMBIC_TABLES`) are overridable via
    explicit carve-out parameters; `default_unmanaged_tables()` combines them.
  - `detect_drift` signature is additive: new `alembic_tables`,
    `exclude_schemas`, `expected_extensions`, `expected_policies`,
    `expected_functions` parameters all default to empty — existing callers
    are unaffected.
  - Error handling in CLI (`check_schema_cmd.py:81-86`): `FerrumConfigError`
    prints the message (config error, not a secret); general `Exception`
    prints only `type(exc).__name__` with a synthetic code — no DSN, password,
    or bound value leak. Verified by the verifier's adversarial string search.
  - `exclude_schemas` guard (`drift.py`): raises `ValueError` if the target
    schema is itself excluded — prevents a contradictory invocation.
- `python/ferrum/cli/check_schema_cmd.py` (148 lines, new):
  - Clean adapter: `run_check_schema` (async) → `dispatch_check_schema`
    (sync Typer wrapper). Exit codes `EXIT_CLEAN=0`, `EXIT_DRIFT=1`,
    `EXIT_CONFIG=2` — matches `inspectdb` convention.
  - Default unmanaged tables split into auth/langgraph/alembic categories with
    independent override; when a category option is omitted, the default
    preset for that category applies.
- `python/ferrum/cli/app.py` (+69 lines): registers `check-schema` Typer
  command with `--schema`, `--exclude-tables`, `--auth-tables`,
  `--langgraph-tables`, `--alembic-tables`, `--include-unmapped`, `--json`,
  `--expected-extensions`. Lazy import of `dispatch_check_schema` inside the
  command body (consistent with other CLI commands).

### Boundary verification

- **No W1-C owned paths touched.** `drift.py` imports nothing from
  `orchestrator.py`, `ledger.py`, or `operations.py`. The task contract
  prohibits editing those; the diff confirms import-only compliance.
  `migrations/introspect.py` is not imported either — `drift.py` issues its
  own `pg_catalog` / `information_schema` queries.
- **No async in Rust, no SQL string-building in Python.** This task is pure
  Python (introspection + comparison + CLI). No Rust crate touched. Compliant
  with §4 (PyO3 boundary) by not touching it.
- **PostgreSQL-only.** `detect_drift` raises `ValueError` for non-postgres
  dialect. The CLI prints a config message and returns `EXIT_CONFIG`. This
  respects §2.6 (PostgreSQL is the only production-readiness target); the
  shipped mysql/sqlite/mssql extras are not extended by W2-F.

### Independent verification cross-check

The verifier (`verification/w2-f-schema-drift-cli/20260829T091235Z.md`) ran
fresh evidence: 26 unit tests, 16 live-PostgreSQL integration tests, CLI
exit-code and JSON-output adversarial checks, `_parse_index_columns` against
6 real `pg_get_indexdef` shapes, default normalization, secret-leak string
search, and scope verification (6 owned files; W2-D/W4-A parallel files are
not W2-F leaks). I accept the verifier's fresh evidence as corroboration of
the architecture claims; I independently inspected the diff for the
architecture-level concerns below.

## Findings

### Non-blocking (comparison-depth gaps against acceptance-criteria sub-items)

1. **(MINOR) Opclass not compared.** The acceptance criteria explicitly lists
   "indexes/opclasses/predicates." `_index_signature` (`drift.py:982`)
   includes (sorted columns, unique, access-method, predicate) but NOT
   opclass. `_parse_index_columns` (`drift.py:772`) explicitly strips the
   opclass suffix. The index introspection query comment (`drift.py:402-403`)
   claims a `pg_opclass` join that is not present in the actual query — the
   query joins `pg_am` for `amname` (access method) only. A
   `varchar_pattern_ops` index would not be detected as drift. The access
   method (btree/gin/gist/brin) IS compared. Smallest correction: add a
   `pg_opclass` join to surface the opclass name, include it in the index
   signature, and stop stripping it in `_parse_index_columns`; or document
   opclass as a known limitation in the module docstring.

2. **(MINOR) FK referenced-table/column drift not detected.**
   `_compare_constraints` (`drift.py:1130-1146`) compares only `delete_rule`
   for FK constraints, not the referenced table/column. The expected side
   (`_expected_constraints`, `drift.py:1078-1079`) sets `foreign_table: None`
   and `foreign_column: None`. A model FK pointing to `table_a` while the
   live FK references `table_b` would not be reported. The `definition`
   string carries the referenced table but is not compared for FKs (only
   CHECK definitions are compared at `drift.py:1150`). Smallest correction:
   compare `foreign_table`/`foreign_column` for FK constraints, or compare
   the `definition` string for FKs as well.

3. **(MINOR) Index column order not preserved in signature.**
   `_index_signature` (`drift.py:987`) sorts columns: `tuple(sorted(...))`.
   For btree indexes, column order is semantically significant (affects
   range-scan eligibility). An index on `(a, b)` would match an index on
   `(b, a)`, masking real drift. Smallest correction: use the ordered tuple
   of columns instead of sorting.

4. **(MINOR) Vector dimension live-path no-op (corroborates verifier finding
   #2).** `_vector_dimensions_from_udt` (`drift.py`) returns
   `column.get("dimensions")`, but the `information_schema.columns` query
   does not populate a `dimensions` key (would require a `pg_attribute` join
   on `atttypmod`). The comparison logic is correct and fires when enriched,
   but no integration test exercises it and the live path is inert. The
   acceptance criterion "vector dimensions" is not met against live
   PostgreSQL. Smallest correction: add a `pg_attribute` join to populate
   `dimensions` for `vector`-type columns, or add a unit test that injects
   a `dimensions` key.

5. **(MINOR) CLI does not expose `exclude_schemas`, `expected_policies`, or
   `expected_functions`.** These are library-only parameters on
   `detect_drift`. The CLI surfaces only `expected_extensions`. RLS policy
   and function *inventory* is reported in JSON, but *comparison* against an
   expected set is reachable only via a programmatic call. Acceptable for
   v0.1 (inventory is the primary CI signal), but the acceptance criteria's
   "RLS policies" and "functions" comparison is library-only from the CLI.
   Smallest correction: add `--expected-policies` / `--expected-functions`
   CLI options in a follow-up, or document the library-only comparison path.

### Non-blocking (documentation/comment accuracy)

6. **(TRIVIAL) Misleading index-query comment.** `drift.py:402-403` states
   "we join via `pg_opclass.oid` to get the name" but the query does not join
   `pg_opclass`. Correct the comment to match the query (which uses
   `pg_get_indexdef` and `pg_am` only).

### Architecturally sound (no finding)

- **Read-only introspection**: all queries are `SELECT`; module and CLI
  docstrings state "never emits or applies DDL." Confirmed by inspection.
- **Bound parameters / no identifier interpolation**: schema name is `$1` in
  every schema-scoped query; table names come from model metadata allowlists
  and CLI options. Respects §2.9.
- **`ferrum_migrations` exclusion**: hardcoded, always excluded.
- **Alembic coexistence**: Alembic tables excluded by default; Ferrum only
  reads and reports; migrations remain authoritative. No `orchestrator.py`/
  `ledger.py`/`operations.py` edits (W1-C boundary respected).
- **JSON for CI**: `to_json()` with `sort_keys=True`, 17 stable top-level
  keys, no secrets/DSN/bound values. Exit codes 0/1/2 for CI integration.
- **Additive evolution**: new dataclass fields and dict keys are additive;
  existing `format_summary()` callers and `column_diffs`/`primary_key_diffs`
  properties are unchanged.
- **Extension-owned function exclusion**: `pg_depend` deptype='e' subquery
  prevents pgvector/pg_trgm functions from surfacing as drift.
- **PostgreSQL 18+ forward-compat**: `contype IN ('u','f','c')` excludes
  `contype='n'` NOT NULL constraints (PG18 implementation detail).

## Decision

**approved**

The drift detection architecture is sound and ratifiable. The design honors
every non-negotiable constraint in §2 (read-only, async-first, PostgreSQL
production target, no raw SQL escape hatches, bound parameters, allowlisted
identifiers) and the W1-C path boundary (no migration-apply edits). The
separation is clean: `drift.py` is a pure library (introspection +
comparison + frozen dataclasses), `check_schema_cmd.py` is a thin CLI
adapter, `app.py` registers the Typer command with lazy import. JSON output
is stable and secret-free for CI. Unmanaged exclusions (Better Auth,
LangGraph, Alembic) are overridable defaults. Alembic coexistence is
correct: Ferrum reads and reports, never applies.

The five minor findings are comparison-depth gaps against specific
acceptance-criteria sub-items (opclass, FK referenced table, index column
order, vector dimension live-path, CLI policy/function expected-set
exposure), not architectural flaws. The architecture supports closing each
gap additively without restructuring. I recommend addressing findings 1-3
(opclass, FK referenced table, index column order) and finding 4 (vector
dimension live-path) in a follow-up before claiming full acceptance-criteria
coverage, but they do not block this architecture gate.

This record grants only the ChiefArchitect gate. It does not substitute for
the CodeReviewer gate or independent verification.
