# Ferrum Product Requirements

## Vision

Ferrum is an async-first ORM for modern Python services that combines Django-style ergonomics, Pydantic v2-native data models, and a Rust-powered SQL engine exposed through PyO3.

Ferrum exists because Python backend teams still make an uncomfortable tradeoff when choosing an ORM:

- SQLAlchemy is powerful and mature, but its model is lower-level than many product teams want for day-to-day CRUD and query composition.
- Tortoise ORM is async-oriented, but does not provide the same performance ambition, migration experience, or Pydantic v2-native model contract Ferrum is targeting.
- Django ORM has excellent developer experience and migrations, but is not async-first and is tied to the Django application model.

Ferrum differentiates by being deliberately narrow at first: async-only, PostgreSQL-only, Pydantic v2-native, and optimized for Python web applications where predictable query behavior, typed models, and productive migrations matter more than broad database coverage.

## Product Principles

- Async-first means no synchronous API, compatibility wrapper, or mixed execution model in v0.1.
- Pydantic v2-native means application models are validation and serialization models, not duplicate ORM-only schemas.
- Familiar does not mean clone: Ferrum should borrow Django QuerySet ergonomics where useful while staying idiomatic for async Python.
- Product value comes from correctness, debuggability, and performance together; a fast ORM that hides failures is not successful.
- v0.1 should prove the core loop before expanding surface area.

## Target Audience

### Primary Persona: Async Python API Developer

As a FastAPI or Starlette developer, I want a model and query API that fits async request handlers naturally, so that I can build PostgreSQL-backed services without dropping into low-level SQL for common workflows.

Needs:

- Awaitable CRUD and query operations.
- Pydantic v2 model compatibility for request, response, and persistence boundaries.
- Clear errors when queries, models, or migrations are invalid.
- Predictable behavior under concurrent web traffic.

### Secondary Persona: Team Migrating From Django ORM

As a backend team migrating from Django to an async service stack, I want familiar QuerySet and migration concepts, so that my team can keep proven product development workflows while moving to FastAPI or Starlette.

Needs:

- QuerySet-style filtering, ordering, limiting, and retrieval.
- Migration workflow inspired by Django, without requiring Django.
- Documentation that maps common Django ORM habits to Ferrum equivalents.

### Secondary Persona: Performance-Sensitive Platform Team

As a platform engineer supporting Python services, I want ORM behavior that is observable and has explicit performance boundaries, so that teams can use Ferrum in production without hidden latency, pool, or query-plan surprises.

Needs:

- Instrumentable query execution.
- Clear failure modes for connection, transaction, validation, and migration errors.
- Benchmarkable performance claims.

## Goals for v0.1

Ferrum v0.1 must make the core developer loop credible:

- Define a Pydantic v2-native model and map it to a PostgreSQL table.
- Perform async CRUD operations through a QuerySet-style API.
- Compose basic filters, ordering, limits, and retrieval operations.
- Generate and execute PostgreSQL SQL through the Rust-powered engine.
- Provide enough migration capability to initialize and evolve simple schemas.
- Surface validation, query, connection, and migration errors clearly.
- Document the intended product position versus SQLAlchemy, Tortoise ORM, and Django ORM.

## Non-Goals

The following are explicitly out of scope for v0.1:

- Synchronous ORM support.
- Multiple database backends.
- SQLite support.
- MySQL, MariaDB, CockroachDB, or other PostgreSQL-compatible dialect support.
- Django application integration.
- Admin UI.
- Full relationship coverage beyond what is required for basic model persistence.
- Advanced query optimization, caching, or automatic prefetch planning.
- Multi-tenant framework features.
- Visual schema designer.

These exclusions apply the MoSCoW lens: async PostgreSQL CRUD, Pydantic v2 models, and basic migrations are Must-have; broader compatibility is Won't-have for v0.1 because it slows validation of the primary job.

## Core Features

### Async QuerySet API

Ferrum must expose an awaitable QuerySet-style API for creating, reading, updating, deleting, filtering, ordering, limiting, counting, and fetching model instances.

Example product intent:

```python
users = await User.objects.filter(is_active=True).order_by("-created_at").limit(10).all()
```

Acceptance criteria:

- All database operations are awaitable.
- Common read paths support `filter`, `order_by`, `limit`, `offset`, `first`, `get`, `all`, and `count`.
- Common write paths support `create`, model save, update, and delete semantics.
- Query errors identify the model, field, and operation involved where possible.
- No sync alternative is documented or exposed.

### Pydantic v2-Native Models

Ferrum models must be defined using Pydantic v2-compatible typing and validation semantics.

Acceptance criteria:

- A model definition is sufficient to describe persisted fields for simple tables.
- Field types use Python type hints and Pydantic v2 validation behavior.
- Model instances can be used naturally at API boundaries without requiring duplicate DTO classes for common cases.
- Validation failures are distinguishable from database and query failures.

### Rust-Powered SQL Engine via PyO3

Ferrum must route performance-sensitive SQL generation and result handling through a Rust-powered engine exposed to Python via PyO3.

Acceptance criteria:

- Query compilation produces PostgreSQL SQL and bind parameters from the Python QuerySet representation.
- Generated SQL is inspectable in development or debugging workflows.
- Engine failures preserve actionable context when crossing the Python/Rust boundary.
- Product documentation avoids promising unsupported database dialects.

### Django-Inspired Migrations

Ferrum must provide a migration workflow inspired by Django's schema evolution model, scoped to simple PostgreSQL schema changes in v0.1.

Acceptance criteria:

- Developers can initialize migration state for a project.
- Developers can generate a migration for simple model field additions, removals, and type changes.
- Developers can apply migrations to a PostgreSQL database.
- Migration errors identify the migration, operation, and database response.
- Destructive or ambiguous changes require explicit developer action.

### PostgreSQL-First Connectivity

Ferrum must support PostgreSQL as the only v0.1 database target.

Acceptance criteria:

- Connection configuration is documented for local development and deployed async services.
- Connection failures produce actionable errors without leaking credentials.
- The product supports concurrent request workloads typical of FastAPI and Starlette services.
- SQLite examples, test shortcuts, and fallback modes are not included in user-facing v0.1 documentation.

### Observability and Debuggability

Ferrum must make ORM behavior understandable during development and production diagnosis.

Acceptance criteria:

- Developers can inspect generated SQL and bind parameters in safe debug contexts.
- Errors distinguish model validation, query construction, SQL execution, connection, transaction, and migration failures.
- Product documentation names expected failure modes and recovery paths.
- Logging or instrumentation hooks are planned before v1.0, even if minimal in v0.1.

## User Stories

### Model Definition

As an async Python developer, I want to define a Ferrum model with Python type hints and Pydantic v2 validation, so that one model can support persistence and API serialization for common service workflows.

Acceptance criteria:

- Given a simple model with scalar fields, Ferrum can infer persisted fields.
- Given invalid input, Ferrum returns a validation error before executing an invalid database write.
- Given a saved row, Ferrum hydrates a typed model instance.

### Async CRUD

As a FastAPI developer, I want to create, retrieve, update, and delete records using awaitable ORM calls, so that database access fits naturally in async route handlers.

Acceptance criteria:

- CRUD examples work without sync wrappers.
- Each operation documents expected return values and error cases.
- Concurrent requests do not require application-level serialization around normal ORM calls.

### Query Composition

As a backend developer, I want to compose filters, ordering, and limits through a QuerySet-style API, so that common product queries remain readable and testable.

Acceptance criteria:

- Queries can be chained without executing until awaited.
- Unsupported lookups fail early with clear messages.
- Generated SQL can be inspected for debugging.

### Migration Workflow

As a team lead migrating from Django ORM, I want migration commands that feel familiar, so that schema changes can be reviewed and applied safely during normal development.

Acceptance criteria:

- Developers can generate and apply simple migrations.
- Migration output is reviewable before execution.
- Destructive changes require explicit confirmation or manual edits.

### Production Diagnosis

As a platform engineer, I want Ferrum failures to be categorized and observable, so that service owners can distinguish bad input, bad queries, database outages, and migration drift.

Acceptance criteria:

- Error types or messages identify the failing layer.
- Connection and execution failures do not expose passwords, tokens, or full connection URLs.
- Debugging guidance includes safe SQL inspection and logging boundaries.

## Prioritization

### Must-Have

- Async-only QuerySet API.
- Pydantic v2-native model definitions.
- PostgreSQL-only execution.
- Rust-powered SQL generation path via PyO3.
- Basic CRUD and query composition.
- Simple migration generation and application.
- Clear error categories for validation, query, connection, execution, and migration failures.

### Should-Have

- SQL inspection for debugging.
- Transaction support for common service workflows.
- Documentation mapping Django ORM concepts to Ferrum equivalents.
- Basic performance benchmarks against representative Python ORM operations.

### Could-Have

- Bulk operations.
- Relationship conveniences beyond the minimum needed for v0.1 examples.
- Instrumentation examples for common observability stacks.
- CLI polish beyond essential migration workflows.

### Won't-Have for v0.1

- Sync support.
- SQLite support.
- Multi-database support.
- Django integration.
- Admin interface.
- Advanced query planner features.

## Success Criteria

Ferrum v0.1 is successful when:

- A developer can build a minimal FastAPI or Starlette service backed by PostgreSQL using Ferrum models and async queries.
- The core README example can be implemented and verified against a real PostgreSQL database.
- At least three representative CRUD/query workflows are documented end-to-end.
- Basic migration workflow is documented and works for simple schema changes.
- Product positioning clearly explains when to choose Ferrum instead of SQLAlchemy, Tortoise ORM, or Django ORM.
- Performance claims are backed by repeatable benchmarks or removed from public messaging.
- No v0.1 documentation implies sync, SQLite, or multi-database support.

Product-level target metrics:

- Time-to-first-successful-query in a new sample app: under 15 minutes for an experienced async Python developer.
- Developer activation: a new user can define a model, run a migration, create a row, and query it from an async route without reading source code.
- Reliability signal: common invalid model, invalid query, and database connection failures produce distinct actionable errors.
- Performance signal: query compilation and hydration benchmarks are measured before v1.0 scope expansion.

## Roadmap

### v0.1: Core Loop

Scope:

- PostgreSQL-only async connectivity.
- Pydantic v2-native model definitions.
- Basic CRUD.
- QuerySet-style filtering, ordering, limits, and retrieval.
- Rust-powered SQL generation path.
- Simple Django-inspired migration workflow.
- Initial documentation and examples.

Exit criteria:

- Core sample app works end-to-end.
- Non-goals remain enforced in documentation and API surface.
- Failure categories are clear enough for early adopters to diagnose common issues.

### v0.2: Service Readiness

Scope:

- Transactions.
- Relationship basics.
- Bulk operations.
- Better query inspection and debug tooling.
- Expanded FastAPI and Starlette examples.

Exit criteria:

- Developers can build a non-trivial service workflow with related models and transactional writes.
- Debug output supports practical production triage without leaking sensitive values.

### v0.3: Migration Confidence

Scope:

- Stronger schema diffing.
- Migration review workflows.
- Safer handling of destructive changes.
- CLI maturity for project and migration operations.

Exit criteria:

- Teams can review, apply, and recover from normal schema evolution workflows with confidence.
- Migration failures are actionable enough for CI and local development.

### v0.4: Observability and Performance

Scope:

- Benchmark suite.
- Query timing and instrumentation hooks.
- Documented performance profiles for common operations.
- Guidance for connection pooling and concurrent request workloads.

Exit criteria:

- Public performance claims are backed by repeatable measurements.
- Production operators have clear guidance for monitoring Ferrum-backed services.

### v1.0: Production Stability

Scope:

- Stable public API.
- Complete documentation for core workflows.
- Compatibility policy.
- Production support guidance.
- Hardened error taxonomy and migration behavior.

Exit criteria:

- Ferrum is ready for production use by teams building async PostgreSQL services.
- Breaking-change policy is explicit.
- Core workflows have tests, examples, and documented failure behavior.

## Dependencies

- Product Designer: translate product requirements into developer experience flows, documentation hierarchy, and onboarding examples.
- Chief Architect: validate feasibility of the product constraints, especially Python/Rust boundary behavior, SQL engine scope, and migration architecture.
- Security Engineer: review credential handling, error redaction, migration safety, and SQL inspection guidance.
- Engineering: estimate and implement only after product requirements, design flow, and architecture review are complete.

## Risks and Tradeoffs

### Scope Risk

Ferrum's value depends on disciplined v0.1 scope. Adding sync support, SQLite, or broad database compatibility too early would dilute the async PostgreSQL job and delay proof of value.

### Concurrency Risk

Async ORM users will run Ferrum inside concurrent web request handlers. The product must make connection behavior, transaction boundaries, and failure isolation explicit enough that developers do not accidentally serialize workloads or leak state across requests.

### Performance Risk

The Rust engine is a differentiator only if it improves real ORM bottlenecks. Ferrum should avoid marketing performance claims until query compilation, SQL generation, and hydration are measured against representative workloads.

### Failure Mode Risk

Crossing Python, Rust, and PostgreSQL can make failures opaque. v0.1 must preserve enough context to distinguish validation errors, query construction errors, PyO3 boundary errors, SQL execution errors, connection failures, and migration drift.

### Adoption Risk

Django-inspired APIs can attract developers but also create expectations Ferrum will not meet in v0.1. Documentation must be direct about what is familiar, what is different, and what is intentionally absent.

## Open Product Questions

- What minimum migration command set is required for v0.1: initialize, generate, apply, rollback, or only initialize/generate/apply?
- Which relationship features, if any, are required before v0.1 can be useful for real sample apps?
- What observability surface is required in v0.1 versus deferred to v0.4?
- Should Ferrum position primarily as a FastAPI companion, a general async Python ORM, or a Django migration path?

## Out-of-Scope Decisions for This Document

- Internal Rust module design.
- PyO3 API shape.
- Connection pool implementation.
- SQL AST architecture.
- Migration file format.
- Package layout.
- Visual identity and website design.
