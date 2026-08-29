---
task_id: w2-c-relations-bulk
run_id: 20260829T110000Z
authority: SecurityEngineer
reviewer: security-engineer-agent
reviewed_at: 2026-08-29T11:30:00Z
base_revision: 22931420c7c7212fe4e9718faa710d9a890ea473
decision: approved
scope:
  - python/ferrum/relations.py
  - tests/python/unit/test_relations.py
  - tests/python/unit/test_bulk_operations.py
  - tests/python/integration/test_relations_bulk.py
---

# Named Authority Verdict

## Authority

SecurityEngineer

## Claims reviewed

- JOIN/prefetch SQL uses allowlisted identifiers and bound values (§2.9,
  §3 SQL safety).
- Bulk write SQL (insert/update/delete/upsert) is safe — no injection
  vector via composite keys, per-row values, conflict predicates, or
  returning.
- Relation access is explicit (§5a) — no hidden I/O that could bypass
  security controls or RLS tenant boundaries.
- No raw SQL escape hatches (no `.raw()`, `.extra()`, string fragments, or
  production-exposed query inspection).
- No credentials, DSNs, bound parameter values, or row data leak through
  prefetch SQL, error messages, or relation descriptors.

## Evidence

### SQL identifier allowlisting (§2.9, §3)

All SQL identifiers in `python/ferrum/relations.py` are derived from
immutable model metadata via `_quote_identifier(identifier, dialect)`
(lines 67-75), which double-quotes for postgres/sqlite, backticks for mysql,
brackets for mssql, and rejects unknown dialects with `FerrumCompileError`.

Identifier sources (all metadata-derived, never user input):

| SQL clause | Source | Line |
|---|---|---|
| `FROM {table}` | `related_meta.table_name` | 569, 634 |
| `WHERE {fk_column} IN (...)` | `rev.fk_column` | 570 |
| `ORDER BY {pk_column}` | `related_pk.column_name` | 571 |
| `SELECT {owner_ident}, {target_ident}` | `_resolve_through_columns` → `metadata.table_name` / `target_meta.table_name` | 631-632 |
| `FROM {through_ident}` | `rel.through_table` | 633 |
| `WHERE {target_pk_ident} IN (...)` | `target_pk.column_name` | 635 |

`_resolve_through_columns` (lines 144-164) derives `owner_col` and
`target_col` from `metadata.table_name` and `target.get_metadata().table_name`
— both produced by `_to_snake_case(class_name)` at class definition time,
never from user input. The docstring (line 152-153) confirms: "Both are
derived from ``table_name`` … — never from user input."

No f-string interpolates a user-supplied identifier:
```
$ grep -nE "f\"(SELECT|INSERT|UPDATE|DELETE|FROM|WHERE|JOIN|ORDER)" python/ferrum/relations.py
576:  sql = f"SELECT * FROM {table} WHERE {fk_column} IN ({placeholders}) ORDER BY {pk_column}"
643:  f"SELECT {owner_ident}, {target_ident} FROM {through_ident} "
644:  f"WHERE {owner_ident} IN ({placeholders})"
667:  f"SELECT * FROM {target_table_ident} "
668:  f"WHERE {target_pk_ident} IN ({placeholders}) "
669:  f"ORDER BY {target_pk_ident}"
```
All interpolated values (`table`, `fk_column`, `pk_column`, `owner_ident`,
`target_ident`, `through_ident`, `target_table_ident`, `target_pk_ident`)
are outputs of `_quote_identifier` over metadata. `{placeholders}` is the
output of `_placeholders(count, dialect)` (lines 78-85), which emits only
`$N` / `?` / `%s` tokens — never values.

### Bound parameter usage (§2.9, §3)

All values travel as bound parameters via `driver.fetch(sql, *batch)`:
- `_prefetch_reverse_fk` line 577: `await driver.fetch(sql, *batch)`
- `_prefetch_m2m` line 646: `await driver.fetch(join_sql, *batch)`
- `_prefetch_m2m` line 671: `await driver.fetch(target_sql, *batch)`

No string interpolation of values into SQL. The `*batch` spread passes
parent IDs / target IDs as positional bound parameters.

`_placeholders` (lines 78-85) emits dialect-correct placeholder tokens:
- postgres: `$1, $2, …`
- mysql: `%s, %s, …`
- sqlite/mssql: `?, ?, …`

### Bulk write SQL safety

Bulk operation IR builders live in `queryset.py` (W1-A/W2-B own, import
only — NOT modified by W2-C). W2-C tests verify the existing builders:

`tests/python/unit/test_bulk_operations.py`:
- `test_rejects_unknown_field` (line 85-88): `_build_bulk_insert_ir` rejects
  unknown fields with `FerrumCompileError` — field allowlisting enforced.
- `test_rejects_inconsistent_columns` (line 90-96): rejects mixed field
  sets.
- `test_conflict_field_validated_against_metadata` (line 219-227): upsert
  conflict fields validated against metadata allowlist — `"ghost" not in
  field_names`.
- `test_upsert_sql_rejects_non_postgres` (line 261-275): upsert rejected
  for non-postgres dialects (defense in depth).

Composite-key validation:
- `test_composite_pk_rejects_wrong_arity` (line 161-164): bulk update
  rejects scalar where composite PK expected.
- `test_composite_pk_rejects_scalar` (line 197-200): bulk delete rejects
  scalar where composite PK expected.

`safe_batch_size` (lines 119-141) is a pure arithmetic helper — no SQL
construction. It clamps batch sizes to keep `num_fields_per_row × batch_size
<= 65535`. No injection surface.

### Relation access explicitness (§5a) — security implication

`python/ferrum/relations.py:187-203` (`install_relation_descriptors`):
Forward descriptors installed for ALL relation kinds. Unloaded access
raises `FerrumRelationNotLoadedError` (line 229-233) — no hidden I/O that
could bypass RLS tenant boundaries or connection-scoped GUCs.

`python/ferrum/relations.py:246-282` (`_ReverseRelationDescriptor`):
- Reverse M2M raises (line 273-277) — no hidden through-table join.
- Reverse FK/OTO returns an unbound `QuerySet` (line 282) with NO I/O —
  the QuerySet requires an explicit `ConnectionLike` at the terminal,
  preserving the tenant-transaction boundary (§5a W1-F contract).

This is security-relevant: hidden I/O on an unloaded relation could
execute a query against a connection that lacks the tenant GUC or
platform-admin scope, leaking data across tenant boundaries. The §5a
contract prevents this by requiring explicit eager loading or an explicit
terminal.

### No raw SQL escape hatches (§2.9)

```
$ grep -nE "\.raw\(|\.extra\(|raw_sql|RawSQL" python/ferrum/relations.py
(no output)
```

No `.raw()`, `.extra()`, string-fragment filters, or production-exposed
query inspection in `relations.py`. All SQL is constructed from
metadata-allowlisted identifiers and bound parameters.

### Credential / secret redaction (§3)

`relations.py` does not handle connection strings, passwords, or DSNs.
The module imports `ConnectionLike` (line 36) and calls
`conn._require_driver()` (lines 568, 630) — it never inspects connection
metadata, DSNs, or credentials. Error messages (`FerrumCompileError`,
`FerrumRelationNotLoadedError`) carry only `model`, `field`, and a
fixed `[FERR-Q407]` code — no bound values, row data, or DSNs.

`FerrumRelationNotLoadedError` (line 229-232) message:
> "Relation {name!r} on {type(obj).__name__} is not loaded. Use
> select_related() or prefetch_related() before accessing it. [FERR-Q407]"

Contains only the relation name and model class name — Ferrum-side
metadata, not user data or credentials.

### Test execution

```
$ uv run pytest tests/python/unit/test_relations.py tests/python/unit/test_bulk_operations.py -x -q
77 passed in 0.23s
```

Security-relevant test coverage:
- `TestExplicitAccess` (test_relations.py:199-239): verifies forward
  relations raise, reverse FK/OTO return unbound QuerySet, reverse M2M
  raises.
- `TestResolvePrefetchName` (test_relations.py:142-170): verifies unknown
  relations rejected, forward FK rejected from prefetch.
- `TestResolveThroughColumns.test_missing_through_table_raises` (line
  185-191): verifies missing through_table fails closed.
- `test_rejects_unknown_field`, `test_conflict_field_validated_against_metadata`
  (test_bulk_operations.py): verify field allowlisting on bulk ops.

## Findings

No security findings. All SQL compilation in `relations.py` uses
metadata-allowlisted identifiers and bound parameters. Relation access is
explicit per §5a — no hidden I/O that could bypass tenant/security
boundaries. No raw SQL escape hatches. No credential or secret exposure.

### Observations (non-blocking)

1. **`_quote_identifier` dialect validation**: the function rejects
   unknown dialects with `FerrumCompileError` (line 75). This is
   defense-in-depth — a caller passing an unsupported dialect fails closed
   before SQL emission. Good.

2. **`conn._require_driver()` access**: `relations.py` accesses the
   private `_require_driver()` method (lines 568, 630). This is consistent
   with existing Ferrum internals and does not expose driver internals
   externally, but a future hardening pass could expose a public
   `conn.driver` property. Not a W2-C security issue.

## Decision

**approved**

The W2-C relation and bulk SQL compilation is safe: all identifiers are
metadata-allowlisted and quoted via `_quote_identifier`, all values are
bound parameters, relation access is explicit per §5a (no hidden I/O that
could bypass security boundaries), and no raw SQL escape hatches exist.
The corrective fix run did not introduce any SQL changes (docstring and
dead-code fixes only).

This record grants only the SecurityEngineer gate. It does not substitute
for the ChiefArchitect, CodeReviewer, or independent verification gates.
