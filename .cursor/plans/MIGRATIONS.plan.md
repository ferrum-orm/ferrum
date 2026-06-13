# Migrations Template — <Migration Capability>

> Template for designing migration-engine behavior. Owner: ChiefArchitect + Backend Engineer;
> migration apply is security-sensitive — notify SecurityEngineer. PostgreSQL only.

## 1. Scope

The migration capability (generation, dry-run, apply, destructive guard, transactionality, etc.).

## 2. Generation

- How planned operations are derived from model/metadata diffs. Classification of each operation:
  safe vs destructive (drop/narrow/`NOT NULL`-on-populated).

## 3. Dry-run

- Mandatory before apply. What the sanitized plan shows (statements, classification). No secrets,
  DSNs, bound values, or row data in output.

## 4. Apply & confirmation gates

- Destructive actions require explicit confirmation. Non-development applies require explicit
  environment confirmation. Unscoped `delete()`/`update()` only via `danger_delete_all()` /
  `danger_update_all()`; default fail.

## 5. Transactionality (ADR-004)

- Default transaction wrapping. The non-transactional exception list (`CREATE INDEX
  CONCURRENTLY`, certain `ALTER TYPE`/enum ops) and the recovery story for partial failure.

## 6. Failure modes & blast radius

- What happens on mid-migration failure (transactional vs non-transactional). Idempotency/retry
  posture. Observability of migration runs (Tier A).

## 7. Security requirements

- Redaction in plan/apply output; confirmation gates; danger APIs. Items to flag for
  SecurityEngineer.

## 8. Tests

- Dry-run required, destructive confirmation, non-dev confirmation, danger APIs, transactional vs
  non-transactional behavior, partial-failure recovery.

## 9. ADR links / open questions

- Dependency on ADR-004; decisions recorded in `DECISIONS.md`.
