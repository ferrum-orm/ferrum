# Ferrum Product Requirements

## Vision

Ferrum is a next-generation async ORM for Python teams building modern services on PostgreSQL. It exists because current Python ORM choices force teams to trade off developer experience, async correctness, type safety, and runtime performance.

Ferrum should feel familiar to developers who like Django's ORM, fit naturally into FastAPI and Starlette applications, use Pydantic v2 as the model boundary, and rely on a Rust-powered SQL engine exposed through PyO3 for performance-critical work.

The product differentiates by being deliberately narrow at first:

- Async-first, with no synchronous compatibility layer in v0.1.
- PostgreSQL-first, with no SQLite or MySQL support in v0.1.
- Pydantic v2 native, with no Pydantic v1 compatibility requirement.
- Django-inspired in API ergonomics without requiring Django as an application framework.
- Rust-powered where speed, query correctness, and schema analysis matter most.

## Problem Statement

Async Python teams want ORM ergonomics without giving up the performance and operational clarity they expect from production services. FastAPI and Starlette users often need a typed model layer, composable queries, predictable migrations, and useful observability, but they do not want to stitch together separate schema, query, validation, and migration systems.

Ferrum v0.1 should prove that a focused, PostgreSQL-only ORM can provide a better default path for async Python services than broad compatibility with many runtimes, databases, and model systems.

## Target Audience

### Primary Persona: Async Python Service Developer

As an async Python developer, I want a typed ORM that integrates cleanly with FastAPI or Starlette, so that I can build database-backed APIs without mixing sync abstractions into my async runtime.

Needs:

- Native `async` and `await` query execution.
- Pydantic v2 model definitions without duplicate schemas.
- Clear query composition with familiar ORM primitives.
- Predictable behavior under concurrent request load.
- Errors that are actionable during local development and production debugging.

### Secondary Persona: Django ORM Migrator

As a developer migrating from Django ORM to async services, I want familiar QuerySet-style APIs, so that I can preserve developer productivity while adopting async-first application frameworks.

Needs:

- Familiar filtering, ordering, limiting, creation, and retrieval flows.
- Migration concepts inspired by Django, adapted for a standalone async ORM.
- Clear documentation for differences from Django ORM.

### Tertiary Persona: Backend Team Lead

As a backend team lead, I want a focused ORM with strong observability hooks and measurable performance, so that my team can adopt it without increasing operational risk.

Needs:

- Structured query and migration telemetry hooks.
- Benchmarkable query compilation, execution, and hydration paths.
- Explicit support boundaries for v0.1.
- Stable enough semantics to evaluate in a real service prototype.

## Goals for v0.1

Ferrum v0.1 must achieve these outcomes:

1. Enable an async Python developer to define Pydantic v2 models and perform basic PostgreSQL CRUD operations with an async QuerySet-style API.
2. Provide a Django-inspired developer experience for common query flows without depending on Django.
3. Use a Rust-powered SQL engine through PyO3 for query compilation, SQL generation, result decoding, schema analysis, and migration planning surfaces where applicable.
4. Establish PostgreSQL as the only supported database target for v0.1.
5. Provide enough observability hooks for teams to trace query execution, inspect generated SQL, and debug failures.
6. Define a migration experience direction that is familiar to Django users, even if advanced migration workflows are deferred.

## Non-Goals

The following are explicitly out of scope for v0.1:

- Synchronous API support.
- SQLite support.
- MySQL or MariaDB support.
- Multi-database routing.
- Pydantic v1 compatibility.
- Django framework integration or dependency on Django internals.
- A full admin interface.
- Advanced relationship loading strategies beyond the v0.1 core scope.
- Distributed transactions or cross-database transactions.
- Visual schema design tooling.
- Production-grade backwards compatibility guarantees before v1.0.

## Core Features

### Async QuerySet API

Ferrum must expose a composable QuerySet-style API for common operations such as create, filter, order, limit, retrieve, update, delete, and list.

User story:

As an async Python developer, I want to query models through an awaitable QuerySet API, so that database work fits naturally inside FastAPI and Starlette request handlers.

Acceptance criteria:

- Query execution methods are awaitable and do not require sync wrappers.
- Basic CRUD operations can be expressed from model classes.
- Filtering, ordering, limiting, and listing can be composed before execution.
- Generated errors identify the model, query operation, and relevant field when possible.

### Pydantic v2 Native Models

Ferrum must use Pydantic v2 as the model definition and validation layer.

User story:

As a backend developer, I want Ferrum models to be Pydantic v2 native, so that my application schema, validation behavior, and database model stay aligned.

Acceptance criteria:

- Model definitions use Pydantic v2 semantics.
- v0.1 documentation states that Pydantic v1 is unsupported.
- Common field types can be mapped to PostgreSQL column types.
- Result hydration returns typed model instances rather than unstructured dictionaries by default.

### Rust-Powered SQL Engine via PyO3

Ferrum must route performance-critical SQL work through a Rust engine exposed to Python via PyO3.

User story:

As a team lead, I want query compilation and hydration to use a Rust-powered engine, so that Ferrum can provide Python ergonomics without accepting Python-only performance ceilings.

Acceptance criteria:

- Product documentation identifies the Rust engine responsibilities at a high level.
- SQL generation is treated as a core product capability, not incidental string concatenation.
- Failure modes from the Rust boundary are surfaced as actionable Python errors.
- Performance benchmarking is defined as a v0.1 success criterion.

### Django-Inspired Migrations

Ferrum must define a migration workflow inspired by Django's developer experience while remaining framework independent.

User story:

As a developer familiar with Django, I want migration concepts that feel recognizable, so that I can manage schema changes without learning a completely foreign workflow.

Acceptance criteria:

- v0.1 defines model-to-schema diffing as a core product direction.
- Migration planning is listed as a Rust engine responsibility.
- The CLI and advanced migration operations may be phased after the first v0.1 slice, but the product direction is documented.
- Migration output should be inspectable before applying changes.

### Observability Hooks

Ferrum must make database behavior visible enough for production service teams to debug and measure.

User story:

As a backend team lead, I want query and migration observability hooks, so that my team can diagnose slow queries, failed SQL generation, and model hydration issues in production.

Acceptance criteria:

- Ferrum exposes hooks or events for query start, query finish, query failure, generated SQL inspection, and hydration failure.
- Hook payloads avoid exposing secrets by default.
- Observability design accounts for high-concurrency async applications.
- Documentation names supported failure modes and the expected diagnostic surface.

## Positioning and Differentiation

### Versus SQLAlchemy

SQLAlchemy is broad, mature, and highly flexible. Ferrum should not try to match its database breadth in v0.1. Ferrum wins when a team values a narrow async-first PostgreSQL path, Pydantic v2 native models, and Django-inspired ergonomics over universal SQL toolkit flexibility.

### Versus Tortoise ORM

Tortoise ORM provides async ORM patterns, but Ferrum should differentiate through Pydantic v2 native modeling, Rust-powered SQL and hydration paths, and stronger positioning around PostgreSQL-first production services.

### Versus Django ORM

Django ORM is productive and familiar, but it is coupled to Django and historically sync-first. Ferrum should borrow the ergonomic lessons without inheriting the framework dependency or sync-first assumptions.

## Prioritization

Using MoSCoW for v0.1:

Must-have:

- Async QuerySet API.
- Pydantic v2 native models.
- PostgreSQL CRUD support.
- Rust-powered SQL generation path.
- Basic generated SQL inspection.
- Clear error surfaces for query and hydration failures.

Should-have:

- Initial model-to-schema diff direction.
- Migration plan preview.
- Query lifecycle observability hooks.
- Basic performance benchmarks against representative CRUD and hydration workloads.

Could-have:

- Bulk operations.
- Relationship support beyond simple references.
- CLI polish for migration workflows.
- Query optimization hints.

Won't-have in v0.1:

- Sync API.
- SQLite or MySQL.
- Pydantic v1.
- Multi-database routing.
- Django app integration.

## Product Success Criteria

Ferrum v0.1 is successful when:

- A developer can build a small FastAPI or Starlette prototype using Ferrum for PostgreSQL-backed CRUD without writing raw SQL for basic flows.
- The first-time model-to-query path can be completed from documentation without framework-specific setup knowledge.
- Pydantic v2 model definitions are sufficient for common typed CRUD use cases.
- Query execution, generated SQL inspection, and hydration failures are observable enough to debug a broken request path.
- Representative CRUD and hydration benchmarks are published with methodology and repeatable commands.
- At least one migration-from-Django-ORM guide or comparison document explains familiar and intentionally different concepts.
- v0.1 scope boundaries are clear enough that engineering can reject out-of-scope requests without product ambiguity.

## Roadmap

### v0.1: Focused Async PostgreSQL Foundation

Scope:

- PostgreSQL support.
- Async query execution.
- Basic CRUD operations.
- QuerySet-style filtering, ordering, limiting, and listing.
- Pydantic v2 native models.
- Rust-powered SQL generation and result hydration path.
- Basic type-safe filters.
- Initial observability hooks.
- Migration direction and schema diff planning surface.

Outcome:

Ferrum can support a realistic async Python service prototype on PostgreSQL with clear product boundaries and measurable performance characteristics.

### v0.2: Production Workflow Expansion

Scope:

- Relationship support.
- Transactions.
- Query optimization.
- Bulk operations.
- More complete migration CLI and migration application workflow.
- Expanded observability and benchmark coverage.

Outcome:

Ferrum becomes suitable for deeper evaluation by backend teams building non-trivial service domains.

### v1.0: Stable Production Release

Scope:

- Production-ready API stability.
- Advanced relationships.
- Mature migration workflows.
- Full documentation.
- Performance benchmarking history.
- Compatibility guarantees for public APIs.
- Operational guidance for failures, tracing, and upgrades.

Outcome:

Ferrum is credible for production adoption in async Python services that are PostgreSQL-first and Pydantic v2 native.

## Dependencies and Risks

Dependencies:

- Product design must define documentation and onboarding flows before public release.
- Architecture must validate the PyO3 boundary, error model, concurrency behavior, and Rust/Python packaging strategy.
- Engineering must define benchmark methodology and observability event contracts.
- Security review is needed before documenting query inspection and observability payload behavior, because generated SQL and hook payloads may contain sensitive values.

Risks:

- A narrow PostgreSQL-only scope may limit early adoption but reduces v0.1 complexity and improves correctness.
- PyO3 packaging and cross-platform distribution can become a product risk if installation is unreliable.
- Django-inspired APIs may create false expectations if differences are not documented explicitly.
- Async concurrency failures can damage trust quickly; cancellation, connection handling, and error propagation need early validation.
- Observability hooks can create performance overhead or accidental data exposure if not scoped carefully.

## Product Lenses Applied

- Jobs-to-be-Done: Ferrum is hired to let async Python teams build PostgreSQL-backed services with ORM productivity, typed models, and production visibility.
- Kano Model: Async correctness, PostgreSQL CRUD, and Pydantic v2 support are must-haves; Rust-backed performance and Django-like ergonomics are performance differentiators; migration polish can become a delighter after the foundation is stable.
- MoSCoW: v0.1 is intentionally constrained to must-have async PostgreSQL workflows and excludes sync, multi-DB, SQLite, MySQL, and Pydantic v1 support.
- Outcome over output: Success is measured by prototype adoption, debuggability, benchmark evidence, and scope clarity, not by the number of ORM features shipped.
- Ruthless prioritization: Broad compatibility is delayed so the core async PostgreSQL path can become reliable sooner.
- Acceptance criteria completeness: Each core feature includes testable acceptance criteria to guide design, architecture, and engineering handoff.
- User story format: Core requirements are expressed as user stories tied to concrete user value.

## Handoff Notes

Product requirements are ready for Product Designer review. Design should focus on documentation/onboarding experience, migration mental model explanation, and developer-facing UX for query inspection, migration preview, and error readability.

Chief Architect should review technical feasibility after design, especially the PyO3 boundary, async concurrency model, Rust/Python error propagation, and observability contract.

Security Engineer should review observability payload requirements before implementation begins to avoid accidental exposure of PII, credentials, or query parameters.
