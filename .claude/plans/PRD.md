# PRD Template — <Feature / Capability>

> Template for a Ferrum product requirements document. The authoritative product contract lives in
> `docs/foundation/PRODUCT_REQUIREMENTS.md`; this template scopes a single feature within it.
> Owner: ProductManager. Fill every section; delete none.

## 1. Summary

One paragraph: what this delivers and for whom (async Python service developers on PostgreSQL).

## 2. Problem & motivation

- The developer pain this solves.
- Why it belongs in v0.1 (or why it is explicitly deferred).

## 3. Goals / Non-goals

- **Goals:** measurable outcomes.
- **Non-goals (YAGNI):** what this explicitly does not do (e.g. no sync API, no multi-DB).

## 4. Users & use cases

- Primary persona and the concrete workflows they perform.

## 5. Functional requirements

- Numbered, testable requirements. Each maps to acceptance criteria below.

## 6. Public API shape (intent)

- The async, Pydantic-v2-native surface developers will call (intent, not implementation).
- Least Astonishment: how it matches developer expectations.

## 7. Security & observability requirements

- SQL allowlist safety, credential handling, Tier A observability default, error taxonomy,
  migration guards — whichever apply. These are release-qualification gates.

## 8. Acceptance criteria

- Bullet list a reviewer can verify objectively. No feature is "done" without tests and (for
  public API) docs.

## 9. Dependencies & open questions

- Upstream/downstream dependencies; open ADRs that gate this (ADR-001..006).

## 10. Out of scope / future

- Explicitly deferred work.
