# Rule: Architecture

> Architecture-level invariants and security gates that bound every change. These are
> release-qualification gates, not suggestions. See `AGENTS.md` §2–§5 and the docs in
> `.claude/docs/`.

## Boundary discipline

- **Python = ergonomics + async + I/O + orchestration.** **Rust = pure compilation + hydration.**
  Do not leak async into Rust or SQL string-building into Python.
- **Single Responsibility / Separation of Concerns:** each module/service has one reason to
  change. Keep compilation, hydration, error mapping, and observability in distinct seams.
- **Evolutionary Architecture:** prefer typed, versioned contracts (the IR) over tight coupling.
- **Least Astonishment:** APIs, models, and conventions behave as developers expect.

## SQL safety (gate)

- User input is never interpolated into SQL identifier or value positions. Identifiers resolve
  only from model-metadata allowlists; values are emitted only as bound parameters.
- Unknown fields, unsupported operators, and invalid sort directions fail with structured errors
  **before** SQL is emitted.

## Credential handling (gate)

- Connection strings, passwords, and secrets never appear in default hook payloads, exceptions,
  logs, or migration dry-run/apply output.
- Connection diagnostics are an allowlist only: host, port, database, username, error category —
  never the password or full DSN.

## Tiered observability (gate)

- **Tier A (default):** query fingerprint, operation/model metadata, duration, status, failure
  category. Bound parameter values never appear under any key.
- **Tier B (normalized SQL)** and **Tier C (full SQL + bound values)** require **Ferrum-specific
  opt-in** and must never activate from a generic `DEBUG=1`. **Tier C is local-dev only** — never
  safe for APM, centralized logs, or production.
- **Observability First:** tracing/metrics/logs (Tier A) are a launch gate, not an afterthought.

## Error boundaries (gate)

- Database errors map to a stable, sanitized Ferrum taxonomy. Raw PostgreSQL `DETAIL`/`HINT`
  containing row data is not exposed by default. PyO3 panics become catchable Python exceptions.

## Migration safety (gate)

- Dry-run is mandatory before apply. Destructive actions (column/table drop, type narrowing,
  `NOT NULL` on a populated column) require explicit confirmation. Non-development applies require
  explicit environment confirmation. Unscoped `delete()`/`update()` require a named danger API
  (`danger_delete_all()` / `danger_update_all()`) and fail by default.

## Design lenses (cite by name in reviews)

CAP tradeoffs · Blast Radius · Data Gravity · Event-Driven vs Request-Response · Schema Evolution
(additive, backward-compatible by default) · Defense in Depth · YAGNI.

## Escalation

Any change to auth, secrets, SQL compilation, or migration apply → **notify SecurityEngineer**.
Architecture decisions, ADRs, service boundaries, data models → **ChiefArchitect**. Do not
implement a feature that bypasses architecture review — stop and flag it.
