---
task_id: w1-b-transactions-retries-locks
run_id: 20260829T114500Z
authority: SecurityEngineer
reviewer: security-engineer
reviewed_at: 2026-08-29T12:30:00Z
base_revision: 71e04328688bf9142751aa8a2c1c59dc1a69b410
decision: approved
scope:
  - python/ferrum/queryset.py
  - tests/python/unit/test_queryset_ir.py
---

# Named Authority Verdict

## Authority

SecurityEngineer

## Claims reviewed

Re-review of SecurityEngineer Finding 1 from the prior verdict
(`20260829T110000Z-security-engineer.md`) against the corrective executor run
(`logs/w1-b-transactions-retries-locks/20260829T114500Z.md`). Scope is narrowly
the corrective fix + tests, not the full W1-B diff (already reviewed in the
prior verdict):

1. `FOR UPDATE OF` table names now escape embedded `"` by doubling, matching
   Rust `Dialect::quote_ident` (§3 defense-in-depth in new SQL emission).
2. No new SQL injection vectors introduced by the fix.
3. No other changes beyond the one-line fix + the four new tests + two import
   additions.

## Evidence

### Corrective fix inspection

`git diff HEAD -- python/ferrum/queryset.py` — the corrective change to
`_append_for_update_clause` is exactly one line (line 309 per the corrective
log):

```python
parts.append("OF " + ", ".join(f'"{t.replace(chr(34), chr(34) * 2)}"' for t in of))
```

This doubles any embedded `"` in the metadata-sourced table name. The
remainder of the `queryset.py` diff (`select_for_update`, `_for_update` state,
`_clone`, `_check_write_scope`, write-terminal `is_write=True` plumbing) is the
original W1-B work reviewed in the prior verdict — no new corrective edits.

### Parity with Rust `Dialect::quote_ident`

`crates/ferrum-sql/src/dialect.rs:61-69`:

```rust
pub fn quote_ident(&self, name: &str) -> String {
    match self {
        Self::Postgres | Self::Sqlite => format!("\"{}\"", name.replace('"', "\"\"")),
        Self::Mysql => format!("`{}`", name.replace('`', "``")),
        Self::Mssql => format!("[{}]", name.replace(']', "]]")),
    }
}
```

For Postgres the Rust path emits `"<name with " doubled>"`. The Python
`_append_for_update_clause` now emits the same shape via
`chr(34)` doubling (`chr(34)` is `"`). Defense-in-depth parity confirmed. The
Rust comment at `dialect.rs:58-59` explicitly states identifiers come from
metadata allowlists and quoting is defense-in-depth, not the primary guard —
the same stance applies to the Python `FOR UPDATE OF` path.

### No new SQL injection vectors

- The `of` list reaching `_append_for_update_clause` is still validated by
  `select_for_update()` against `metadata.relations` field names or the
  literal `"self"` (resolved to `metadata.table_name` / `rel_meta.table_name`)
  before the dict is stored on the QuerySet — unchanged by the fix.
- `nowait` / `skip_locked` are boolean flags appending literal `NOWAIT` /
  `SKIP LOCKED` keywords — no user input.
- Non-postgres dialects are rejected with `FerrumConfigError` before any SQL
  emission (`_append_for_update_clause` raises `[FERR-C001]`).
- The `FOR UPDATE` clause takes no bound values (correct — it accepts no
  parameters).
- The fix strictly *adds* escaping; it cannot weaken the existing
  metadata-allowlist guard (§2.9, §3). No new code path, no new identifier
  source, no new value interpolation.

### Corrective scope is fix + tests only

`git diff HEAD -- tests/python/unit/test_queryset_ir.py` — exactly:
- 2 import additions (`FerrumConfigError`, `_append_for_update_clause`).
- New class `TestAppendForUpdateClauseQuoteEscaping` with 4 tests:
  - `test_simple_table_name_quoted` — `["article"]` → `OF "article"`.
  - `test_multiple_table_names_quoted` — comma-join of multiple `OF` targets.
  - `test_embedded_double_quote_doubled` — `bad"name` → `"bad""name"` (the
    defense-in-depth fix under test).
  - `test_non_postgres_dialect_rejected` — mysql raises `FerrumConfigError`.

No other test or source changes. Matches the corrective log's "Changed files"
section exactly.

### Test output (requested command)

```
$ uv run pytest tests/python/unit/test_queryset_ir.py -x -q -k "for_update or quote or escape"
....                                                                     [100%]
4 passed, 73 deselected in 0.19s
```

All four targeted tests pass, including `test_embedded_double_quote_doubled`
which fails on the pre-fix code (`bad"name` would emit `"bad"name` — broken
quoting) and passes on the post-fix code (`"bad""name"`).

### Security regression sweep

```
$ uv run pytest tests/python/security/test_sql_safety.py -x -q -m security
74 passed in 0.29s
```

No regressions in the SQL-safety security suite.

## Findings

None. Finding 1 from the prior verdict is resolved:

- **FINDING 1 (prior, minor — defense-in-depth): RESOLVED.** The
  `_append_for_update_clause` now doubles embedded `"` in `FOR UPDATE OF`
  table names, matching Rust `Dialect::quote_ident` for Postgres. The primary
  §2.9/§3 guard (identifiers from metadata allowlists only; no user input in
  identifier positions) was never in question and remains intact. The
  defense-in-depth surface is now consistent across the Python and Rust
  quoting paths.

No new findings introduced by the corrective fix.

## Decision

approved

Finding 1 is resolved. The `FOR UPDATE OF` table-name quoting now escapes
embedded `"` by doubling, achieving parity with Rust
`Dialect::quote_ident`. The fix is minimal (one line), covered by four new
unit tests (including a regression test for the embedded-quote case), and
introduces no new SQL injection vectors. The security suite (74 tests) and
the targeted quote-escaping tests (4 tests) pass. The SecurityEngineer gate
for W1-B is cleared.

This record grants only the SecurityEngineer gate. It does not substitute for
ChiefArchitect or CodeReviewer authority, or for independent verification.
