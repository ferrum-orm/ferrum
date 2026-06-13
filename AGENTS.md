# AGENTS.md — Coding Agent Guidance for Ferrum

> Authoritative guidance for AI coding agents (Cursor and any `AGENTS.md`-aware tool)
> working in this repository. Claude Code reads `CLAUDE.md`, which defers to this file
> for the shared rules below. Treat this document as the single source of truth for
> how to build Ferrum. When this file and a deeper `.cursor/rules/*` file disagree, the
> more specific rule wins; otherwise this file governs.

## 1. What Ferrum is

Ferrum is a **next-generation async ORM for Python** with a **Rust-powered core**,
**Pydantic v2-native models**, and a **Django-inspired developer experience**. It targets
modern async Python services (FastAPI / Starlette) that need type-safe, observable,
PostgreSQL-backed persistence without a synchronous compatibility layer.

Read these before doing substantial work — they are the product and architecture contract:

- `docs/foundation/PRODUCT_REQUIREMENTS.md` — the v0.1 product contract (scope, security, acceptance criteria).
- `docs/foundation/ARCHITECTURE_FEASIBILITY_REVIEW.md` — the architecture verdict, invariants, and the ADRs that must exist.
- `docs/foundation/SECURITY_REVIEW_PRD.md` — security requirements that are release-qualification gates.
- `docs/design/PRODUCT_DESIGN_REVIEW.md` — developer-experience and onboarding decisions.
- `README.md` — the external pitch and the API shape we are committing to.

## 2. Non-negotiable architectural constraints

These are product- and architecture-level invariants. **Do not violate them, and do not
"temporarily" violate them to make something compile.** If a task seems to require breaking
one, stop and surface it (see §9 Escalation).

1. **Python owns public developer ergonomics.** The public API, model definitions, QuerySet
   surface, async runtime, connection pool, transactions, and hook dispatch live in Python.
2. **Rust owns performance-critical internals only.** The Rust core is a **pure, synchronous,
   stateless compiler/codec**: `QuerySet IR → {sql_text, bound_params, param_type_summary}` and
   `raw rows → hydrated payload`. Rust stays **off the async I/O path**.
3. **Async-first only.** Every core API is awaitable. **No synchronous API, sync wrapper, or
   blocking compatibility layer** in the v0.1 MVP.
4. **Pydantic v2 first.** Model definitions are the single source of truth for validation,
   serialization, and persistence. No duplicate persistence schemas.
5. **PyO3 + maturin** are the Rust↔Python bridge and build tool. The boundary maps Rust
   `Result::Err` and panics to **catchable Python exceptions** — never a process abort,
   memory address, or local path leak.
6. **PostgreSQL is the first and only supported database** for the MVP. **No multi-database
   abstraction, no SQLite shortcut, no MySQL fallback** before the PostgreSQL MVP is stable.
7. **No ORM feature ships without tests.** A feature without tests is not done.
8. **Public API changes require documentation updates** in the same change.
9. **No raw SQL escape hatches** (no `extra()`, string fragments, user-supplied templates, or
   production-exposed query inspection). SQL identifiers resolve only from model-metadata
   **allowlists**; values are emitted only as **bound parameters**.
10. **No per-request mutable shared state in Rust.** Model metadata is built once at class
    definition time and is thereafter read-only. Compilation is a pure function over
    `(&Metadata, QuerySetIR)` producing fresh owned output per call.

## 3. Security rules (release-qualification gates, not suggestions)

Ferrum treats SQL compilation, observability payloads, error surfaces, and migration
execution as product-level security scope. Any change touching these MUST keep these true,
and they MUST be covered by tests:

- **SQL safety:** user input is never interpolated into SQL identifier or value positions.
  Unknown fields, unsupported operators, and invalid sort directions fail with structured
  errors **before** SQL is emitted.
- **Credential handling:** connection strings, passwords, and secrets never appear in default
  hook payloads, exceptions, logs, or migration dry-run/apply output. Connection diagnostics
  are limited to an allowlist (host, port, database, username, error category) — never the
  password or full DSN.
- **Tiered observability:** default hook payloads are **Tier A only** (query fingerprint,
  operation/model metadata, duration, status, failure category). Bound parameter values never
  appear in default payloads under any key. Tier B (normalized SQL) and Tier C (full SQL +
  bound values) require **Ferrum-specific opt-in** and must never activate from a generic
  `DEBUG=1`. Tier C is local-dev only and never safe for APM, centralized logs, or production.
- **Error boundaries:** database errors map to a stable, sanitized Ferrum taxonomy. Raw
  PostgreSQL `DETAIL`/`HINT` containing row data is not exposed by default. PyO3 panics become
  catchable Python exceptions.
- **Migration safety:** dry-run is mandatory before apply. Destructive actions (column/table
  drop, type narrowing, `NOT NULL` on a populated column) require explicit confirmation.
  Non-development applies require explicit environment confirmation. Unscoped `delete()` /
  `update()` must require a named danger API (`danger_delete_all()` / `danger_update_all()`)
  and fail by default.

**Any change to auth, secrets, SQL compilation, or migration apply must be flagged for
SecurityEngineer review.** Do not self-clear security-sensitive changes.

## 4. The PyO3 boundary (how Python and Rust interact)

- The IR crossing the boundary is a **typed, versioned, serializable contract**. Values are
  carried **out-of-band from identifiers** so parameterization and allowlisting are structural,
  not convention.
- The Rust compile call is **synchronous and holds the GIL** — compilation is CPU-bound and
  sub-millisecond; do not release/reacquire the GIL for it, and do not put cancellable waiting
  inside Rust. All cancellation/timeout handling lives in Python at the driver await point.
- Build with `panic = "unwind"` for the extension; wrap the boundary so a Rust panic surfaces
  as a catchable Python exception. Error payloads carry **structured fields** (model, field,
  operator, category) — never formatted trace blobs.
- Hydration: Rust constructs typed payloads from trusted DB-origin rows. The default uses the
  Pydantic v2 **construct-without-revalidate** fast path (DB already enforced types). Document
  the trusted-source assumption and the custom-validator caveat (see ADR-003).

## 5. Open architecture decisions (ADRs) — do not pre-empt them

The architecture feasibility review enumerates six ADRs that must be resolved in the
architecture docs before the relevant implementation begins. Until an ADR is decided, **do not
hard-code a choice that forecloses it.** If your task depends on an undecided ADR, surface it.

- **ADR-001** PostgreSQL driver placement (Python-side `asyncpg` is the default leaning).
- **ADR-002** QuerySet→Rust IR contract shape and version stability.
- **ADR-003** Hydration semantics (construct-without-revalidate vs. full validation).
- **ADR-004** Migration transactionality + the non-transactional exception list (`CREATE INDEX CONCURRENTLY`, certain `ALTER TYPE`/enum ops).
- **ADR-005** Packaging targets & CI wheel matrix (maturin + cibuildwheel, abi3).
- **ADR-006** Centralized, non-bypassable error-mapping + hook-payload/redaction layer.

## 6. Repository layout (current and planned)

- `docs/foundation/` — PRD, architecture feasibility review, security review. Authoritative.
- `docs/design/` — product design review.
- `.cursor/` — Cursor agent config: `rules/`, `skills/`, `commands/`, `plans/` (plans use the
  `*.plan.md` suffix). Set up by the workspace-structure task.
- `.claude/` — Claude Code agent config: `rules/`, `skills/`, `commands/`, `plans/` (plans use
  plain `*.md`).
- Production source (Python package + Rust crate) is **not yet implemented** and must not be
  added by workspace-setup or documentation tasks.

When source lands, expect roughly:

- `python/ferrum/` (or `src/ferrum/`) — the public Python package.
- `rust/` (a maturin-managed crate) — the Rust compiler/codec core.
- `tests/` — Python tests; Rust unit tests live with the crate.
- `pyproject.toml` + `Cargo.toml` — Python and Rust build manifests.

## 7. How to work in this repo

- **Read the contract first.** Ground every change in the PRD + architecture review. If a
  request conflicts with them, the documents win; flag the conflict rather than silently
  diverging.
- **Prefer minimal, reviewable diffs.** Do not rewrite working modules to restyle them. Make
  the smallest change that satisfies the task and its tests.
- **Stay inside the boundary.** Put I/O, async, and orchestration in Python; put pure
  compilation/hydration in Rust. Do not leak async into Rust or SQL string-building into Python.
- **Tests are part of the change.** New behavior → new tests in the same diff. A bug fix →
  a regression test that fails before and passes after.
- **Public API change → docs change.** Update `README.md` and any affected docs in the same
  change. A public API change without docs is incomplete.
- **Errors must be actionable.** Validation, compilation, and migration errors must be
  understandable without reading Ferrum source, and must not echo submitted values or secrets.
- **Observability is a launch gate, not an afterthought.** Anything touching the query path
  must preserve the Tier A default hook contract and the redaction layer.
- **No speculative complexity (YAGNI).** Do not add relationship loaders, sharding, multi-DB
  abstractions, sync wrappers, or config knobs that v0.1 does not require.

## 8. Definition of done

A change is done only when all of the following hold:

- [ ] It honors every constraint in §2 and every security rule in §3.
- [ ] It does not pre-empt an undecided ADR in §5.
- [ ] It has tests that cover the new/changed behavior (and security-relevant paths where applicable).
- [ ] Public API changes are reflected in `README.md` / docs in the same change.
- [ ] Errors are sanitized and actionable; no secrets, DSNs, bound values, or row data leak by default.
- [ ] The diff is minimal and scoped to the task.
- [ ] Lint/format/type checks (Python) and `cargo check`/`clippy` (Rust) pass for touched code.

## 9. Escalation

- **Product requirement decisions** (what to build, scope changes) → ProductManager.
- **Visual / developer-experience decisions** → ProductDesigner.
- **Architecture decisions, ADRs, service boundaries, data models** → ChiefArchitect.
- **Auth, secrets, SQL-compilation, or migration-apply changes** → notify SecurityEngineer.
- **Cost/risk decisions (e.g., CI wheel matrix breadth) or board-level technology choices** →
  ChiefArchitect escalates to CEO.

Do not implement a feature that bypasses architecture review. If you find implementation
proceeding without an approved architecture for the affected area, stop and flag it to the
ChiefArchitect.
