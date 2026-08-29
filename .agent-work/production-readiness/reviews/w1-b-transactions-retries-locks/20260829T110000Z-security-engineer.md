---
task_id: w1-b-transactions-retries-locks
run_id: 20260829T085649Z
authority: SecurityEngineer
reviewer: security-engineer
reviewed_at: 2026-08-29T11:00:00Z
base_revision: 71e04328688bf9142751aa8a2c1c59dc1a69b410
decision: changes_required
scope:
  - python/ferrum/runtime.py
  - python/ferrum/connection.py
  - python/ferrum/queryset.py
---

# Named Authority Verdict

## Authority

SecurityEngineer

## Claims reviewed

Security-gated claims per task contract `security_surfaces.sql_compilation: true`
and `security_review_justification: "statement-retry scope and select_for_update
SQL are security-gated"`:

1. Retry scope cannot cause write duplication (autocommit writes never retry;
   `Transaction` never retries).
2. `select_for_update` SQL uses metadata-allowlisted identifiers and bound
   parameters only (§2.9, §3 SQL safety).
3. Advisory lock keys are validated — no arbitrary SQL injection via lock keys.
4. `run_transaction` retry cannot duplicate writes (fresh transaction per
   attempt, callback must be idempotent).
5. `connection` category removed from `_RETRY_CATEGORIES` (per §5a — not valid
   for DML retry).
6. No SQL injection vectors in any new SQL emission.

## Evidence

### Diff inspection

Inspected `git diff HEAD -- python/ferrum/runtime.py python/ferrum/connection.py
python/ferrum/queryset.py` against AGENTS.md §3 (Security rules), §5a (ratified
retry contract — binding), and §2.9 (no raw SQL).

**Retry scope (claims 1, 5):**
- `_RETRY_CATEGORIES` reduced to `{"deadlock", "serialization"}` — `connection`
  removed (`runtime.py:50`). Matches §5a.
- `RetryPolicy.__post_init__` rejects any `on` category not in
  `_RETRY_CATEGORIES`, including `connection` (`runtime.py:95-102`). A caller
  cannot construct a `RetryPolicy(on={"connection"})`.
- `_exception_category` returns only `"deadlock"`, `"serialization"`, or `None`
  (`runtime.py:52-66`). Confirmed by grep: no `return "connection"`, no
  `PostgresConnectionError` reference remains. (Verifier Finding 2 is stale —
  already resolved in the current diff.)
- `TimedQueryExecutor._execute_with_policy` sets `retry = None` when
  `self._is_transaction or is_write` (`runtime.py:390-391`).
- `execute` is always `is_write=True` (`runtime.py:417`).
- `Transaction._require_driver()` passes `is_transaction=True`
  (`connection.py:803-808`).
- QuerySet write terminals with RETURNING pass `is_write=True` on their
  `fetch`/`fetchrow` calls (`queryset.py:2269, 2639, 3019, 3119, 3439`).

**`select_for_update` (claim 2):**
- `of` entries validated against `metadata.relations` field names or literal
  `"self"`; resolved to `metadata.table_name` / `rel_meta.table_name`
  (`queryset.py:1776-1817`). `resolve_relation` (`relations.py:167-175`) only
  matches `rel.field_name == name` — no arbitrary string handling.
- `nowait`/`skip_locked` are boolean flags appending literal `NOWAIT` /
  `SKIP LOCKED` keywords — no user input.
- Non-postgres dialects rejected with `FerrumConfigError` before SQL emission
  (`queryset.py:291-295`).
- Rejected in `_check_write_scope` for UPDATE/DELETE/write terminals
  (`queryset.py:3189-3193`).
- FOR UPDATE clause takes no bound values (correct — it accepts no parameters).

**Advisory locks (claim 3):**
- `AdvisoryLockKey` rejects `bool` (subclass of int), non-int, bad tuple length,
  and overflow (64-bit int, 32-bit tuple halves) (`runtime.py:179-217`).
- `as_args()` returns validated ints only.
- All advisory lock SQL uses `$N` positional parameters with explicit casts
  (`$1::bigint` or `$1::int, $2::int`) — no identifier interpolation
  (`connection.py:974-998`).

**`run_transaction` (claim 4):**
- Opens a fresh `self.transaction(...)` per attempt inside the `while True`
  loop (`connection.py:660-666`). Each attempt is a new transaction.
- On retriable `FerrumError` → rollback (via `async with` context manager) +
  backoff + `continue` (new transaction) (`connection.py:667-672`).
- On non-retriable `FerrumError` → `raise` after rollback
  (`connection.py:670-671`).
- On `asyncio.CancelledError` → `raise` after rollback, no retry
  (`connection.py:674-677`).
- `TransactionRetryPolicy.should_retry` restricts to `exc.sqlstate in
  {"40001", "40P01"}` and requires `isinstance(exc, FerrumError)`
  (`runtime.py:154-164`). Non-Ferrum exceptions (including `CancelledError`)
  are never retried.
- Callback idempotency documented as caller responsibility in the docstring
  (`connection.py:run_transaction`).
- Atomicity: rollback always precedes the next attempt → no write duplication
  from the retry mechanism itself.

**Deadline docstring (verifier Finding 1):** Current docstring states "per
attempt" / "the budget is not tracked across attempts" (`connection.py` diff).
Implementation passes the same `deadline` to each `self.transaction()` call.
Docstring and implementation now match. From a security lens this is not a
write-duplication risk: each attempt is atomic (rollback before retry),
regardless of whether the deadline budget is per-attempt or total.

### Security tests

```
$ uv run pytest tests/python/security/test_sql_safety.py -x -q -m security
74 passed in 0.32s
```

(Note: running without `-m security` deselects all 74 — the `security` marker
is required. The bare command in the task prompt yields `74 deselected`.)

### Identifier quoting comparison

Rust `Dialect::quote_ident` for Postgres escapes embedded `"` by doubling:
`name.replace('"', "\"\"")` (`crates/ferrum-sql/src/dialect.rs:61-69`), with a
unit test asserting `"bad""name"` for input `bad"name`. The Rust comment
states this is "defense-in-depth, not the primary guard" — the primary guard
is that identifiers come exclusively from model metadata allowlists.

The Python `_append_for_update_clause` uses `f'"{t}"'` with NO escaping
(`queryset.py:_append_for_update_clause`). This is inconsistent with the Rust
defense-in-depth layer.

## Findings

### FINDING 1 (minor — defense-in-depth): `FOR UPDATE OF` table-name quoting does not escape embedded double quotes

- **Severity:** minor
- **Evidence:** `queryset.py:_append_for_update_clause` constructs the `OF`
  clause with `', '.join(f'"{t}"' for t in of)`. The `t` values come from
  `metadata.table_name` (model metadata allowlist) — NOT user input. The
  primary §2.9/§3 guard (no user input in identifier positions) holds.
  However, the Python path does not escape embedded `"` by doubling, unlike
  the Rust `Dialect::quote_ident` (`crates/ferrum-sql/src/dialect.rs:61-69`)
  which is explicitly documented as defense-in-depth for the same
  metadata-sourced identifiers. A malformed `Meta.table = 'foo"; ...'`
  (developer error) would break out of quoting in the `FOR UPDATE OF` clause
  specifically, while the same malformed table name in the main
  SELECT/UPDATE/DELETE would be safely escaped by Rust `quote_ident`. This is
  an inconsistent defense-in-depth surface in new SQL emission.
- **Affected criterion:** Claim 2 / Claim 6 (no SQL injection vectors in new
  SQL emission). Not a §3 violation (no user input reaches the identifier),
  but a defense-in-depth gap in new SQL emission that §3 treats as a
  release-qualification gate.
- **Required correction:** Escape embedded `"` by doubling in
  `_append_for_update_clause` to match Rust `quote_ident`, e.g.
  `f'"{t.replace(chr(34), chr(34)*2)}"'`. Alternatively, route `FOR UPDATE`
  through the Rust IR/compiler so it uses the centralized `quote_ident`.

### Verifier findings re-examined (no security defect)

- **Verifier Finding 1 (deadline docstring):** Already resolved in the current
  diff — docstring says "per attempt" / "not tracked across attempts",
  matching the implementation. Not a security issue: per-attempt deadline
  cannot cause write duplication (rollback is atomic per attempt).
- **Verifier Finding 2 (dead `return "connection"`):** Already resolved —
  `_exception_category` no longer returns `"connection"` and has no
  misleading comment (`runtime.py:52-66`, confirmed by grep). Clean per §5a.

## Decision

changes_required

Finding 1 is a minor defense-in-depth gap in new SQL emission. The primary
§3 SQL-safety guard (no user input in identifier positions; metadata
allowlist) holds, so this is not a blocking §3 violation. The fix is trivial
(one-line quote-doubling to match Rust `quote_ident`) and should be applied
before the workstream transitions to `verified`. After Finding 1 is fixed,
the security gate is cleared.

This record grants only the SecurityEngineer gate. It does not substitute
for ChiefArchitect or CodeReviewer authority, or for independent
verification.
