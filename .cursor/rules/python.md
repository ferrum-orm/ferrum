# Rule: Python

> Constraints for all Python code in Ferrum (the public package and its tests). Python owns
> developer ergonomics, async orchestration, and I/O. See `AGENTS.md` §2.

## Scope ownership

Python owns: public API, model definitions, QuerySet surface, async runtime integration,
connection pool, transactions, hook dispatch, error mapping, migration orchestration. CPU-bound
SQL compilation and row hydration are delegated to Rust — do **not** build SQL strings or
re-implement hydration in Python.

## Hard constraints

- **Async-first.** Every public core API is `async`/awaitable. No synchronous API, sync wrapper,
  or `asyncio.run`-style blocking shim in library code.
- **Pydantic v2 native.** Models subclass the Ferrum/Pydantic v2 base. The model is the single
  source of truth for fields, types, validation, and serialization. Never define a parallel
  persistence schema.
- **No raw SQL escape hatches.** No string SQL fragments, `extra()`, f-string interpolation into
  queries, or user-supplied templates. Field/identifier names resolve only through
  model-metadata allowlists; values cross the boundary as bound parameters.
- **Errors are sanitized and actionable.** Raise from Ferrum's structured exception taxonomy.
  Never echo submitted values, DSNs, secrets, or raw driver `DETAIL`/`HINT` row data by default.
- **Type-checked.** Public surfaces are fully type-annotated and must pass the configured type
  checker. No `# type: ignore` without a one-line justification.

## Style and tooling

- Format and lint with the repo's configured tooling (e.g. `ruff` / `black`); run on touched
  files, not the whole tree, unless scope warrants.
- Prefer explicit, readable code over cleverness. Optimize for debuggability and observability.
- No comments that merely narrate code. Comments explain non-obvious intent, trade-offs, or
  constraints only.

## Concurrency & failure modes (call these out explicitly)

- Cancellation and timeouts are handled in Python at the driver await point — never inside Rust.
- Connection-pool acquisition must be bounded and cancellation-safe; document pool exhaustion
  behavior.
- Hooks run on the query path: keep them cheap and never let a hook failure corrupt the query
  lifecycle. Default hook payloads are **Tier A only** (see `architecture.md` / security gates).

## Definition of done (Python)

- [ ] Async, type-annotated, lint/format clean on touched files.
- [ ] New behavior covered by tests; bug fixes get a regression test.
- [ ] Public API change → docs updated in the same change.
- [ ] No secrets/values/DSNs leak in errors, logs, or hook payloads.
