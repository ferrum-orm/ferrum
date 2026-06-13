# Ferrum Product Requirements

## Vision

Ferrum is an async-first ORM for modern Python services that combines Django-inspired ergonomics, Pydantic v2-native models, and a Rust-powered SQL engine exposed through PyO3. It exists to avoid the productivity vs. async vs. type-safety trade-offs that FastAPI, Starlette, and other async framework teams currently accept when wiring persistence layers.

## Problem Statement

Async Python teams need a persistence layer that feels familiar enough to move fast, respects their async runtime, and keeps validation/DB schemas aligned. Current choices force compromises: SQLAlchemy feels lower-level, Tortoise lacks the same type guarantees and production positioning, and Django ORM is tied to a synchronous, monolithic framework. Ferrum's job is to prove a focused async PostgreSQL path that keeps developer delight and operational clarity in one package.

## Target Audience

### Primary Persona: Async Python Service Developer

As a FastAPI or Starlette developer, I want an ORM that executes with `async`/`await`, so my route handlers never mix sync blocking calls with database work.

Needs:

- Awaitable CRUD, query, and migration APIs.
- Pydantic v2 model definitions that double as validation/serialization schemas.
- Predictable PostgreSQL behavior and failure messages.
- Clear guidance for concurrent request workloads and observability hooks.

### Secondary Persona: Django ORM Migrator

As a team migrating from Django, I want familiar QuerySet and migration semantics so that productivity carries over without adopting the entire framework.

Needs:

- Chainable filters, ordering, limits, and retrieval that echo Django QuerySets.
- A migration workflow inspired by Django but framework-agnostic.
- Explicit documentation highlighting differences from Django ORM.

### Tertiary Persona: Platform/Performance Lead

As a platform engineer, I want observable database behavior and measurable performance so the team can trust Ferrum in production.

Needs:

- Structured hooks for query timing, execution outcomes, and migration diagnostics.
- Benchmarks and instrumentation examples that stay within async request budgets.
- Scope clarity before teams say yes to adoption.

## Jobs To Be Done

- When building an async PostgreSQL API, teams hire Ferrum to turn typed Python models into safe database reads and writes without leaving their async runtime.
- When migrating from Django ORM workflows, teams hire Ferrum to keep developer productivity while adopting FastAPI or Starlette.
- When operating production services, teams hire Ferrum to make database access observable, debuggable, and predictable under load.

## Product Principles

- **Async-first.** No synchronous compatibility wrappers or hidden blocking behavior in v0.1; every core API is awaitable.
- **Pydantic v2 native.** Model definitions are the single source of truth for validation, serialization, and persistence.
- **Narrow scope, high leverage.** PostgreSQL-only, Django-inspired ergonomics, Rust engine performance, and observability hooks before broad database coverage.
- **Outcome over output.** Success is measured by prototype activation, diagnosable failures, and repeatable benchmarks, not feature count.

## Goals for v0.1 (MoSCoW)

### Must-have

- Async PostgreSQL CRUD using a QuerySet-style API.
- Pydantic v2 model definitions that drive schema and validation.
- Rust/PyO3-powered SQL generation that emits parameterized queries.
- Basic migration plan generation and apply workflow for supported schema changes.
- Query observability hooks that capture duration, SQL context, and failure classification.
- Documentation that compares Ferrum to SQLAlchemy, Tortoise ORM, and Django ORM.

### Should-have

- Common filter operators (exact, contains, comparisons, null checks, boolean logic).
- Clear error messages with field/query/model context without leaking secrets.
- Migration dry-run output and reviewable SQL.
- Benchmarks that surface async query and hydration performance baselines.

### Could-have

- FastAPI or Starlette starter examples that stitch models, migrations, and async routes.
- A lightweight transaction helper API if it does not delay the CRUD foundation.
- Structured OpenTelemetry instrumentation beyond simple hooks.

### Won't-have (v0.1)

- Synchronous APIs, sync wrappers, or SQLite adapters.
- MySQL, MariaDB, or multi-database targets.
- Full Django compatibility, admin UI, or complex prefetch behavior.
- Sharding, replication control, or connection proxying.

## Non-Goals

Ferrum v0.1 explicitly avoids:

- Sync engines or blocking compatibility layers.
- Multi-database targeting (PostgreSQL only).
- SQLite or framework-specific adapters.
- Replacing full web frameworks, admin UIs, or auth stacks.
- Deep relationship loading, polymorphic relations, or automatic prefetch planning.

## Core Features

### Async QuerySet and CRUD

Ferrum must expose an awaitable QuerySet-style API for create, read, update, delete, filter, order, limit, and retrieve operations.

Product requirements:

- Terminal operations (`create`, `get`, `first`, `all`, `count`, `update`, `delete`) execute only when awaited.
- Chain methods (`filter`, `exclude`, `order_by`, `limit`, `offset`) are composable without hitting the database.
- No synchronous alias or blocking helper is introduced in v0.1.

Acceptance criteria:

- Developers can `await Model.objects.create(...)` to insert and hydrate a typed model.
- Queries with filters, ordering, and limits execute when awaited and return typed results.
- Unsupported fields/operators raise actionable errors before or during SQL compilation.
- Execution failures surface model/query context without leaking raw row data.

### Pydantic v2 Native Models

Model definitions align with Pydantic v2 so validation, serialization, and persistence share the same schema.

Product requirements:

- Field definitions use Python type hints and Pydantic v2 validation semantics.
- Defaults, optional fields, and validators behave consistently with Pydantic v2.
- Ferrum metadata (tables, columns) wraps Pydantic without requiring duplicate schema declarations.

Acceptance criteria:

- Supported scalar fields derive PostgreSQL column metadata from a single model definition.
- Invalid input raises field-specific validation errors before generating SQL.
- Developers do not need separate persistence schemas for supported v0.1 models.
- Model hydration returns typed Pydantic instances by default.

### Rust-Powered SQL Engine (Via PyO3)

Ferrum routes performance-sensitive SQL generation and hydration through a Rust core exposed via PyO3.

Product requirements:

- The Rust engine compiles QuerySet representations into parameterized PostgreSQL SQL.
- No user input is interpolated into SQL strings.
- Structured compilation errors bubble up as understandable Python exceptions.
- Performance trade-offs remain transparent; opaque failures are unacceptable.

Acceptance criteria:

- Supported queries emit parameterized SQL and bound parameter lists.
- Unsupported query shapes raise structured errors rather than malformed SQL.
- Concurrent compilation reuses immutable state and never mutates shared request data.

### Django-Inspired Migrations

Ferrum provides a migration workflow inspired by Django while remaining framework-neutral.

Product requirements:

- Generate migration plans from model schema diffs.
- Expose dry-run output showing SQL before applying.
- Apply migrations to PostgreSQL via documented commands or API with safety guards.
- Migration results are deterministic for the same schema state.

Acceptance criteria:

- New models produce migrations that create expected PostgreSQL tables.
- Field additions/removals/types generate describable schema changes.
- Dry-run requests return the planned SQL without mutating the database.
- Unsafe or unsupported migration actions fail with explicit guidance.

### Observability & Failure Visibility

Ferrum must make production failure modes diagnosable and avoid leaking sensitive data.

Product requirements:

- Hooks/events for query start, success, failure, SQL inspection, and hydration failure.
- Default logging avoids raw bound parameter values.
- Documentation covers failure categories for validation, compilation, connection, execution, and migrations.
- Concurrency expectations for async queries are explicit so developers know what can be shared.

Acceptance criteria:

- Developers can record duration and context for each query.
- Failure classification distinguishes validation, compilation, and execution issues.
- Concurrent awaits on independent queries do not mutate shared compiled state.
- Migration failure messages specify whether the database changed before failure.

### PostgreSQL-First Connectivity & Execution

Ferrum throttles scope to PostgreSQL in v0.1 to guarantee correctness and clarity.

Product requirements:

- Connection settings documented for local and deployed environments.
- Query execution handles PostgreSQL-specific behaviors (timeouts, cancellations).
- Connection failures surface actionable errors without exposing credentials.
- No SQLite shortcuts or multi-database fallbacks appear in user-facing docs.

Acceptance criteria:

- Connection configuration works via explicit Python config or env vars.
- Timeouts, cancellations, and connection drops emit deterministically classified errors.
- Observability hooks document PostgreSQL behavior for deployments.

## User Stories

### Model Definition

As an async Python developer, I want to define database-backed models using Pydantic v2-style annotations, so that validation and persistence schemas stay aligned.

Acceptance criteria:

- Scalar fields declared with Python type annotations appear in derived persistence metadata.
- Defaults and nullability behave predictably at create time.
- Validation failures reference the offending field.
- Unsupported fields raise actionable product-level errors.

### Async CRUD

As an async Python developer, I want to create, retrieve, update, and delete records through awaitable ORM calls, so that request handlers stay non-blocking.

Acceptance criteria:

- `create`, `get`, `first`, `all`, `update`, and `delete` work with `await`.
- Typed not-found/multiple-result errors exist for `get`.
- Bulk updates/deletes have documented return values.
- Unscoped destructive operations require explicit safety calls.

### Query Composition

As a backend developer, I want to compose filters, ordering, and limits through a QuerySet-like API, so that common queries feel readable.

Acceptance criteria:

- Chains support the documented operator subset.
- Ordering handles ascending/descending across supported fields.
- Limit and offset push down predictably.
- Invalid filters fail before hitting SQL.

### Migration Planning

As a Django-experienced developer, I want schema changes to produce reviewable migration plans, so that database changes can be inspected safely.

Acceptance criteria:

- Migration generation detects supported model diffs.
- Dry-run output lists operations and SQL.
- Apply commands record enough state to avoid double-applying.
- Unsupported schema changes fail with next-action guidance.

### Observability

As an operator, I want query timing and errors to be observable, so that production issues can be triaged without exposing sensitive data.

Acceptance criteria:

- Hooks describe query start, success, failure, and SQL context.
- Hook payloads exclude raw parameter values by default.
- Error categories differentiate validation, compilation, connection, execution, and migration failures.

## Success Criteria

- A new developer can go from Ferrum documentation to a running FastAPI/Starlette prototype with async PostgreSQL CRUD in under 30 minutes.
- The v0.1 API supports the CRUD, filter, order, limit, and migration paths needed by a simple service.
- Documentation clearly describes when to choose Ferrum over SQLAlchemy, Tortoise ORM, or Django ORM.
- Benchmarks cover query compilation, execution, and hydration baselines without regressions during release qualification.
- Validation, query compilation, and migration planning errors are actionable without referencing Ferrum source code.
- Observability hooks supply duration, SQL context, and failure category in logs or metrics.
- At least one end-to-end example demonstrates Pydantic v2 models, async queries, PostgreSQL migrations, and FastAPI or Starlette integration.

## Positioning

### Versus SQLAlchemy

Ferrum is narrower and more opinionated; SQLAlchemy stays the flexible, mature choice for complex DB work. Ferrum wins when teams want a Django-like async ORM with Rust-assisted performance and stronger typing.

### Versus Tortoise ORM

Ferrum is more explicit about Pydantic v2, migration direction, observability, and PostgreSQL-first correctness. Tortoise remains a lightweight async ORM; Ferrum wins when type alignment and production discipline matter.

### Versus Django ORM

Ferrum is framework-neutral and async-first. Django ORM stays tied to Django and synchronous execution. Ferrum wins when teams want the ergonomic lessons without the framework dependency.

## Roadmap

### v0.1 - Core Async PostgreSQL Loop

Scope:

- PostgreSQL-only async connectivity and execution.
- Pydantic v2 model definitions.
- Async QuerySet CRUD, filter, order, limit, and list.
- Rust-powered SQL generation via PyO3.
- Migration planning, dry-run, and apply workflow for supported fields.
- Query timing and error classification hooks.
- Documentation for positioning and quickstart.

Outcome:

- Prove a reliable async ORM path for a small production-shaped service.

### v0.2 - Service Readiness

Scope:

- Relationship support for common one-to-many/many-to-one patterns.
- Explicit transaction helper APIs.
- Bulk operations.
- Query optimization and instrumentation improvements.
- Expanded FastAPI/Starlette examples.

Outcome:

- Make Ferrum viable for more realistic service domains without expanding database scope.

### v0.3 - Migration Confidence

Scope:

- Schema diff engine polish.
- CLI tooling and migration history inspection.
- Safer diagnostics for destructive operations.
- Developer workflow polish.

Outcome:

- Make schema evolution trustworthy enough for teams adopting Ferrum across multiple services.

### v1.0 - Production Stability

Scope:

- Public API stability guarantees.
- Advanced relationships and migration maturity.
- Performance benchmarking/gates.
- Comprehensive documentation and operational guidance.

Outcome:

- Establish Ferrum as a dependable ORM choice for async Python teams running PostgreSQL-backed services in production.

## Dependencies

- Chief Architect review for Rust/PyO3 engine boundary, concurrency model, migration architecture, and PostgreSQL connection strategy.
- Product Designer review for documentation architecture, onboarding, and developer experience flows.
- Security Engineer review before documenting query inspection, observability payloads, or migration outputs that touch secrets.
- Engineering input on benchmark targets, supported Pydantic v2 subset, and migration safety guarantees.

## Risks & Tradeoffs

- PostgreSQL-first focus improves correctness but delays SQLite or MySQL teams.
- Async-only positioning differentiates but excludes sync workloads and Django-native scripts.
- Rust/PyO3 tooling adds packaging, build, and debugging complexity.
- Django-inspired semantics risk overpromising compatibility.
- Migration support must be conservative; unsafe behavior damages trust faster than a narrower subset.
- Observability hooks add early value but can slow the foundation release if scope grows.

## Open Questions

- What minimum migration command set should v0.1 expose (init/generate/apply/rollback)?
- Which relationship helpers, if any, are strategically required before v0.1 is useful for real sample apps?
- What subset of observability hooks must land in v0.1 versus v0.4?
- Should Ferrum position primarily as a FastAPI companion, a general async Python ORM, or a Django migration path?
- What packaging targets must the Rust/PyO3 engine support in the first release?

## Scope Exclusions Summary

- No synchronous ORM APIs or wrappers in v0.1.
- No SQLite, MySQL, or multi-database support.
- No admin interface, advanced relationships, or Django framework dependency.
- No architecture decisions beyond the product constraints documented here.

## Handoff Notes

Product requirements are ready for Product Designer review to define the documentation/onboarding story, query inspection UX, and migration preview experience. After design sign-off, Chief Architect should validate the Rust/PyO3 boundary, async concurrency guarantees, and migration tooling before engineering ramps implementation. Security Engineer must clear any observability/migration payload that touches secrets before release.
