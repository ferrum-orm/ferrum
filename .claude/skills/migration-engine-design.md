# Skill: Migration Engine Design

> Expert behavior for the schema migration engine. Use when designing migration generation,
> dry-run, apply, or destructive-change guards. Migration apply is security-sensitive — notify
> SecurityEngineer.

## When to use

- Designing migration generation from model/metadata diffs.
- Implementing dry-run, apply, and confirmation gates.
- Designing transactionality and the non-transactional exception list (ADR-004).

## Expert behaviors

- **Dry-run is mandatory.** No apply without a preceding dry-run that shows the exact planned
  statements. Dry-run/apply output must be free of secrets, DSNs, bound values, and row data.
- **Destructive changes are gated.** Column/table drops, type narrowing, and `NOT NULL` on a
  populated column require explicit confirmation. Non-development applies require explicit
  environment confirmation.
- **Unscoped mutations need a named danger API.** `delete()`/`update()` without a scope must fail
  by default; only `danger_delete_all()` / `danger_update_all()` proceed, and they are explicit.
- **Transactionality is deliberate (ADR-004).** Wrap migrations in transactions by default;
  maintain an explicit exception list for operations that cannot run transactionally
  (`CREATE INDEX CONCURRENTLY`, certain `ALTER TYPE`/enum ops). Document recovery for partial
  failure of non-transactional steps.
- **PostgreSQL only.** Target PostgreSQL semantics; no multi-DB abstraction.

## Workflow

1. Diff model metadata → planned operations; classify each as safe vs destructive.
2. Render a dry-run plan (sanitized) and require confirmation for destructive/non-dev applies.
3. Implement apply with transaction wrapping + the non-transactional exception path.
4. Add tests for every guard: dry-run required, destructive confirmation, danger APIs, non-dev
   confirmation, partial-failure behavior.

## Anti-patterns

- Applying without dry-run, or auto-confirming destructive changes.
- Echoing row data, DSNs, or secrets in plan/apply output.
- Allowing unscoped `delete()`/`update()` to run by default.
- Assuming all statements are transactional (they are not).
