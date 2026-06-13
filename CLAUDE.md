# CLAUDE.md — Claude Code Guidance for Ferrum

> Guidance for Claude Code working in this repository.
>
> **`AGENTS.md` is the single source of truth** for Ferrum's architecture, security, and
> engineering rules. Read it first and treat its constraints as binding. This file restates the
> must-not-break rules for quick reference and adds Claude-Code-specific operating notes. When
> `CLAUDE.md` and `AGENTS.md` differ on a shared rule, **`AGENTS.md` wins** — update `AGENTS.md`,
> then mirror it here, so the two never drift.

## Start here

1. Read `AGENTS.md` (this repo's authoritative agent contract).
2. Read `docs/foundation/PRODUCT_REQUIREMENTS.md` and
   `docs/foundation/ARCHITECTURE_FEASIBILITY_REVIEW.md` before substantial work.
3. Check `.claude/rules/` for any deeper, file-scoped rules. More specific rules win.

## What Ferrum is

A next-generation **async ORM for Python** with a **Rust-powered core**, **Pydantic v2-native
models**, and a **Django-inspired developer experience**, targeting async PostgreSQL services
(FastAPI / Starlette). See `AGENTS.md` §1 and `README.md`.

## Non-negotiable constraints (summary — full text in `AGENTS.md` §2–§5)

- **Python owns developer ergonomics; Rust owns performance-critical internals only.** Rust is a
  **pure, synchronous, stateless** compiler/codec and stays off the async I/O path.
- **Async-first only** — no sync API or blocking compatibility layer in the MVP.
- **Pydantic v2 first** — models are the single source of truth for validation, serialization,
  and persistence.
- **PyO3 + maturin** bridge Rust and Python; the boundary maps `Err`/panics to catchable Python
  exceptions, never a process abort or leaked internals.
- **PostgreSQL only** for the MVP — no multi-database abstraction, no SQLite/MySQL fallback
  before the PostgreSQL MVP is stable.
- **No feature without tests.** **Public API changes require docs updates** in the same change.
- **No raw SQL escape hatches.** Identifiers come from model-metadata allowlists; values are
  bound parameters only.
- **No per-request mutable shared state in Rust.** Metadata is built once and read-only.

## Security gates (full text in `AGENTS.md` §3)

These are release-qualification gates and must be test-covered:

- No user input interpolated into SQL; bad fields/operators/sort directions fail before SQL emission.
- No credentials, DSNs, bound values, or row data in default hooks, errors, logs, or migration output.
- Default observability is **Tier A only**; Tier B/C are explicit Ferrum opt-ins (never `DEBUG=1`),
  and bound-value (Tier C) inspection is local-dev only.
- Errors map to a sanitized Ferrum taxonomy; PyO3 panics are catchable.
- Migrations: mandatory dry-run; destructive actions and non-dev applies require explicit
  confirmation; unscoped `delete()`/`update()` require a named danger API.

**Flag any change to auth, secrets, SQL compilation, or migration apply for SecurityEngineer
review.** Do not self-clear security-sensitive work.

## Open ADRs — do not pre-empt (full text in `AGENTS.md` §5)

ADR-001 driver placement · ADR-002 IR contract · ADR-003 hydration semantics ·
ADR-004 migration transactionality · ADR-005 packaging/CI matrix · ADR-006 centralized
error/hook layer. If your task depends on an undecided ADR, surface it instead of hard-coding a
choice.

## Working in this repo (Claude Code specifics)

- **Explore before editing.** Use search/read tools to ground changes in the PRD and
  architecture review. Do not assume APIs that are not yet specified.
- **Plan large or ambiguous work.** Put planning artifacts in `.claude/plans/` as plain `*.md`
  (the matching Cursor location uses the `*.plan.md` suffix). Do not stop at a plan if the task
  is actionable — implement, then leave a clear next step.
- **Prefer minimal diffs.** Make the smallest change that satisfies the task and its tests; do
  not rewrite working code to restyle it.
- **Run the smallest verification that proves the change** — touched-file lint/type/tests over
  full-repo builds, unless the task scope warrants more.
- **Production source is not implemented yet.** Do not add ORM source as part of
  documentation, workspace-setup, or planning tasks.
- **Git commits:** if you commit, add exactly `Co-Authored-By: Paperclip <noreply@paperclip.ing>`
  to the end of the commit message.

## Definition of done (full checklist in `AGENTS.md` §8)

Honor §2 constraints and §3 security rules; don't pre-empt §5 ADRs; include tests; update docs
for public API changes; keep errors sanitized and actionable; keep the diff minimal; pass
Python lint/format/type checks and Rust `cargo check`/`clippy` for touched code.

## Escalation (full map in `AGENTS.md` §9)

Product scope → ProductManager · DX/visual → ProductDesigner · architecture/ADRs/data models →
ChiefArchitect · auth/secrets/SQL/migration → notify SecurityEngineer · cost/risk or board-level
technology choices → ChiefArchitect escalates to CEO. Never implement a feature that bypasses
architecture review — stop and flag it.
