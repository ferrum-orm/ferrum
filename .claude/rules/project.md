# Rule: Project — Ferrum

> Strict, always-applied constraints for all work in this repository. `AGENTS.md` at the
> repo root is the single source of truth; this rule restates the project-level invariants
> that must never be broken. When this rule and `AGENTS.md` differ, **`AGENTS.md` wins**.

## What Ferrum is

Ferrum is a **next-generation async ORM for Python** with a **Rust-powered core**,
**Pydantic v2-native models**, and a **Django-inspired developer experience**. It targets
async Python services (FastAPI / Starlette) needing type-safe, observable, PostgreSQL-backed
persistence — with **no synchronous compatibility layer**.

## Read before substantial work

- `AGENTS.md` — authoritative agent contract.
- `.claude/docs/PRODUCT_REQUIREMENTS.md` — v0.1 product contract.
- `.claude/docs/ARCHITECTURE.md` — architecture contract: invariants, boundaries, ADRs.
- `.claude/docs/SECURITY.md` — release-qualification security gates.
- `.claude/docs/PRODUCT_DESIGN.md` — developer-experience decisions.

## Non-negotiable invariants

1. **Python owns public developer ergonomics** — public API, models, QuerySet surface, async
   runtime, pool, transactions, hook dispatch.
2. **Rust owns performance-critical internals only** — a pure, synchronous, stateless
   compiler/codec. Rust stays off the async I/O path.
3. **Async-first only** — no sync API, sync wrapper, or blocking compatibility layer in v0.1.
4. **Pydantic v2 first** — models are the single source of truth for validation, serialization,
   and persistence. No duplicate persistence schemas.
5. **PyO3 + maturin** bridge Rust and Python.
6. **PostgreSQL only** for the MVP — no multi-database abstraction, no SQLite/MySQL fallback
   before the PostgreSQL MVP is stable.
7. **No feature ships without tests.**
8. **Public API changes require docs updates** in the same change.
9. **No raw SQL escape hatches** — identifiers from model-metadata allowlists; values as bound
   parameters only.
10. **No per-request mutable shared state in Rust** — metadata built once, read-only thereafter.

## Working posture

- Prefer minimal, reviewable diffs. Do not restyle working code.
- Ground every change in the PRD + architecture review; if a request conflicts, the docs win —
  flag it rather than silently diverging.
- No speculative complexity (YAGNI). No relationship loaders, sharding, multi-DB abstractions,
  sync wrappers, or config knobs that v0.1 does not require.
- Production source (Python package + Rust crate) is **not yet implemented**; do not add ORM
  source as part of documentation, workspace-setup, or planning tasks.

## Open ADRs — do not pre-empt

ADR-001 driver placement · ADR-002 IR contract · ADR-003 hydration semantics ·
ADR-004 migration transactionality · ADR-005 packaging/CI matrix · ADR-006 centralized
error/hook layer. If a task depends on an undecided ADR, surface it instead of hard-coding a choice.
