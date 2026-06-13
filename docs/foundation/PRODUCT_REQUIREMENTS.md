# Ferrum Product Requirements

## Vision

Ferrum is an async-first ORM for Python teams building modern API services. It exists to remove the common tradeoff between a productive ORM experience, native async execution, strongly typed application models, and high-performance database access.

Ferrum is differentiated by combining:

- A Django-inspired ORM experience for developers who value readable model and query code.
- Native async behavior from the first release, without a synchronous compatibility layer.
- Pydantic v2 native models so application validation and persistence models do not drift.
- A Rust-powered SQL engine, exposed to Python through PyO3, for query compilation, SQL generation, result decoding, schema analysis, and migration planning.
- PostgreSQL-first scope so the v0.1 product can be reliable before it becomes broad.

Ferrum should feel like the ORM an experienced Django user would want when building a FastAPI or Starlette service in 2026: familiar enough to learn quickly, async enough to fit the runtime, typed enough to trust in large codebases, and fast enough to use in latency-sensitive paths.

## Problem Statement

Async Python teams often choose between:

- SQLAlchemy: powerful and mature, but with a learning curve and patterns that can feel lower-level than product teams want for everyday CRUD workflows.
- Tortoise ORM: async-oriented and approachable, but less differentiated on type-safety, migration workflow, and performance positioning.
- Django ORM: extremely productive and familiar, but coupled to Django's synchronous heritage and not a natural fit for FastAPI or Starlette services.

The product opportunity is to give async Python developers a familiar ORM that makes the common path fast, typed, observable, and production-ready without asking them to adopt a full web framework.

## Target Audience

### Primary Persona: Async API Developer

As an async Python developer building FastAPI or Starlette services, I want a productive ORM that matches my async runtime, so that I can model data, run queries, and ship API features without mixing blocking database patterns into the service.

Needs:

- Simple model definitions using Python typing and Pydantic v2 semantics.
- Awaitable CRUD and query APIs.
- Predictable PostgreSQL behavior.
- Clear error messages when query or model definitions are invalid.
- Good enough performance for request-path database access.

### Secondary Persona: Django ORM Migrator

As a backend engineer moving selected services away from Django, I want familiar ORM concepts in an async-first package, so that the team can preserve developer productivity while adopting FastAPI, Starlette, or service-oriented runtimes.

Needs:

- QuerySet-like chaining.
- Familiar filter, ordering, limit, create, update, and delete semantics.
- Migration concepts that do not require learning a wholly unrelated workflow.
- Explicit documentation of differences from Django ORM behavior.

### Secondary Persona: Technical Lead

As a technical lead responsible for production reliability, I want an ORM with clear performance, observability, migration, and failure-mode guarantees, so that the team can adopt it without hiding operational risk.

Needs:

- Measurable latency and throughput targets.
- Instrumentation hooks for query timing and error visibility.
- Safe migration planning behavior.
- Documented concurrency and transaction behavior before production adoption.

## Jobs To Be Done

- When building an async API backed by PostgreSQL, developers hire Ferrum to turn typed Python models into safe database reads and writes without leaving the async runtime.
- When migrating from Django-style workflows, teams hire Ferrum to preserve ORM productivity while adopting a framework-neutral async stack.
- When operating a production service, teams hire Ferrum to make database access observable, debuggable, and predictable under load.

## Goals For v0.1

Ferrum v0.1 must prove that the core product thesis works for a narrow, valuable path.

### Must-Have Goals

- Developers can define Pydantic v2 native models that map to PostgreSQL tables.
- Developers can create, retrieve, update, delete, filter, order, limit, and list records through an async QuerySet-style API.
- Query construction is type-aware enough to catch unsupported fields, operators, and value shapes before sending invalid SQL when practical.
- The Rust-powered SQL engine can compile the supported query subset into parameterized PostgreSQL SQL.
- Basic migrations can produce and apply schema changes for supported model definitions.
- The library exposes query timing and error hooks suitable for application logs and metrics.
- Documentation explains positioning against SQLAlchemy, Tortoise ORM, and Django ORM.

### Should-Have Goals

- The API supports common filter operators such as exact match, contains, greater-than, less-than, in-list, null checks, and boolean checks.
- Errors include enough model, field, and query context for fast debugging without exposing sensitive row data.
- v0.1 includes benchmark baselines against representative SQLAlchemy async and Tortoise ORM workflows.
- Migration commands support dry-run output so teams can inspect SQL before applying it.

### Could-Have Goals

- First-class FastAPI examples.
- Basic transaction helper API if it can be specified without delaying the CRUD/query foundation.
- Structured OpenTelemetry integration, beyond simple instrumentation hooks.

### Won't-Have Goals

- Synchronous database access.
- SQLite support.
- MySQL, MariaDB, or other non-PostgreSQL databases.
- Full Django compatibility.
- Admin UI.
- Complex relationship traversal.
- Sharding, replication management, or connection proxying.

## Non-Goals

The following are explicitly out of scope for v0.1:

- Sync support: Ferrum does not provide synchronous adapters or blocking compatibility wrappers.
- Multi-database support: PostgreSQL is the only supported database target.
- SQLite support: local development uses PostgreSQL-compatible workflows, not SQLite substitution.
- General web framework: Ferrum is not a replacement for FastAPI, Starlette, Django, or Flask.
- Full Django ORM parity: Ferrum borrows ergonomic concepts, not complete API compatibility.
- Advanced relationship modeling: nested eager loading, polymorphic relations, and complex prefetch behavior are later roadmap items.
- Production schema governance: approval workflows, deployment orchestration, and data backfills are outside the ORM's v0.1 scope.

## Core Features

### Async QuerySet API

Ferrum exposes an awaitable QuerySet-style API for the supported CRUD and read-query path.

Product requirements:

- Query construction is lazy until an awaitable terminal method is called.
- Supported terminal methods include `create`, `get`, `first`, `all`, `count`, `update`, and `delete`.
- Supported chain methods include `filter`, `exclude`, `order_by`, `limit`, and `offset`.
- The API does not provide sync aliases or implicit blocking behavior.

Acceptance criteria:

- Given a valid model and PostgreSQL connection, a developer can create a record with `await Model.objects.create(...)`.
- Given existing rows, a developer can filter, order, limit, and fetch records using an awaitable chain.
- Given an unsupported field or operator, Ferrum returns a clear product error before or during query compilation.
- Given a database execution failure, Ferrum surfaces the failing model/query context without including raw secrets or full row payloads.

### Pydantic v2 Native Models

Ferrum models are built around Pydantic v2 so validation, typing, and application model behavior stay aligned.

Product requirements:

- Model fields use Python type annotations compatible with Pydantic v2.
- Defaults and validation behave consistently with documented Pydantic v2 semantics.
- Ferrum-specific table and field metadata is additive and does not require duplicate schema definitions.

Acceptance criteria:

- Given a model with typed fields and defaults, Ferrum can derive the persistence schema for supported field types.
- Given invalid field input, validation errors are attributable to the relevant model field.
- Given a supported Pydantic v2 model definition, developers do not need to define a separate persistence schema by hand.

### Rust-Powered SQL Engine Via PyO3

Ferrum uses a Rust-powered engine, callable from Python through PyO3, for performance-sensitive database work. This is a product requirement and a technical constraint to be reviewed by the Chief Architect before implementation.

Product requirements:

- The engine compiles the supported query representation into parameterized PostgreSQL SQL.
- SQL generation must avoid string interpolation of user-provided values.
- The engine reports structured compilation errors that Python callers can render as actionable developer messages.
- Performance work must preserve debuggability; opaque failures are not acceptable even if the fast path is faster.

Acceptance criteria:

- Given a supported filter/order/limit query, the engine emits parameterized SQL and bound parameters.
- Given an unsupported query shape, the engine returns a structured error instead of malformed SQL.
- Given repeated query compilation under concurrent async use, compilation remains deterministic and does not share mutable request state unsafely.

### Django-Inspired Migrations

Ferrum provides a migration workflow inspired by Django while remaining framework-neutral.

Product requirements:

- Developers can generate migration plans from supported model schema changes.
- Developers can inspect generated SQL before applying a migration.
- Developers can apply migrations to PostgreSQL through a documented command or API.
- Migration output must be deterministic for the same model state.

Acceptance criteria:

- Given a new supported model, Ferrum can generate a migration that creates the expected PostgreSQL table.
- Given a supported field addition or removal, Ferrum can generate a migration plan that describes the schema change.
- Given a dry-run request, Ferrum shows the planned operations without mutating the database.
- Given an unsafe or unsupported migration operation, Ferrum fails closed with a clear message.

### Observability And Failure Modes

Ferrum v0.1 must be operable in production-like services even before v1.0 stability.

Product requirements:

- Expose query timing, query outcome, and error classification hooks.
- Avoid logging bound parameter values by default.
- Document expected behavior for connection failures, transaction failures, query compilation failures, validation failures, migration planning failures, and migration apply failures.
- Document concurrency expectations for async queries, including what state is safe to share across tasks.

Acceptance criteria:

- Given a successful query, applications can record duration and model/query category.
- Given a failed query, applications can classify the failure without parsing raw exception strings.
- Given concurrent awaits on independent query objects, one query cannot mutate the compiled state of another.
- Given a migration failure, Ferrum reports whether the database was mutated before failure when that can be known.

## User Stories

### Model Definition

As an async Python developer, I want to define database-backed models using Pydantic v2 style annotations, so that my validation and persistence schemas stay aligned.

Acceptance criteria:

- Supported scalar fields can be declared with Python type annotations.
- Defaults are respected when creating records.
- Validation errors point to the model field that caused the failure.
- Unsupported field types fail with an actionable message.

### Create And Read Records

As an async Python developer, I want to create and fetch records through awaitable ORM methods, so that request handlers can use the ORM without blocking the event loop.

Acceptance criteria:

- `create` inserts a valid record and returns a hydrated model instance.
- `get` returns exactly one matching record or a typed not-found/multiple-results error.
- `first` returns one matching record or `None`.
- `all` returns a list or documented collection type of hydrated model instances.

### Filter And Order Queries

As a developer building API endpoints, I want to filter and order records with a readable chainable API, so that endpoint query logic stays concise and reviewable.

Acceptance criteria:

- Filters support the documented v0.1 operator subset.
- Ordering supports ascending and descending order on supported fields.
- Limit and offset apply predictably to result sets.
- Invalid filters fail before executing unsafe or malformed SQL.

### Update And Delete Records

As a backend engineer maintaining application data, I want to update and delete records through async ORM calls, so that common data mutations do not require hand-written SQL.

Acceptance criteria:

- Updates can be scoped by supported filters.
- Deletes can be scoped by supported filters.
- Bulk update/delete behavior is explicitly documented, including return values.
- Unscoped destructive operations require an explicit documented call pattern or safety acknowledgement.

### Generate And Inspect Migrations

As a technical lead, I want schema changes to produce reviewable migration plans, so that database changes can be inspected before they affect shared environments.

Acceptance criteria:

- Migration generation detects supported model changes.
- Dry-run output includes planned operations and SQL where available.
- Applying a migration records enough state to avoid accidental duplicate application.
- Unsupported schema changes fail with a clear next action.

### Observe Query Behavior

As an operator of an async Python service, I want query timing and errors to be observable, so that database issues can be debugged in production without exposing sensitive data.

Acceptance criteria:

- Applications can attach hooks or callbacks for query start, success, and failure events.
- Events include model/query category and duration.
- Events exclude raw parameter values by default.
- Error categories distinguish validation, compilation, connection, execution, and migration failures.

## Success Criteria

Ferrum v0.1 is successful when:

- A new developer can follow documentation to define a model, connect to PostgreSQL, run a migration, and perform basic CRUD in under 30 minutes.
- The v0.1 query API covers the top-level CRUD and list/filter/order paths needed by a simple FastAPI service.
- The product documentation clearly explains when to choose Ferrum versus SQLAlchemy, Tortoise ORM, or Django ORM.
- The supported query path is benchmarked and does not regress below agreed baseline targets during release qualification.
- Error messages from validation, query compilation, and migration planning are actionable without reading Ferrum source code.
- Observability hooks allow service owners to capture query duration and failure category in application logs or metrics.
- At least one end-to-end example demonstrates Pydantic v2 models, async queries, PostgreSQL migrations, and FastAPI or Starlette integration.

## Positioning

### Versus SQLAlchemy

Ferrum should be positioned as more opinionated and productively ORM-first for async API developers. SQLAlchemy remains the mature, highly flexible choice for complex database use cases. Ferrum wins when teams want a narrower, typed, Django-like async ORM path with Rust-assisted performance.

### Versus Tortoise ORM

Ferrum should be positioned as more explicit about Pydantic v2 native modeling, PostgreSQL-first correctness, migration workflow, observability, and Rust-powered performance. Tortoise remains a lightweight async ORM option. Ferrum wins when teams want stronger type/model alignment and a more ambitious production path.

### Versus Django ORM

Ferrum should be positioned as framework-neutral and async-first. Django ORM remains the productivity standard inside Django applications. Ferrum wins when teams want Django-like ergonomics in FastAPI, Starlette, or other async services without adopting Django as the application framework.

## Roadmap

### v0.1 - Product Thesis Release

Scope:

- PostgreSQL support.
- Pydantic v2 native models.
- Async QuerySet API for basic CRUD.
- Supported filter/order/limit query subset.
- Rust-powered SQL compilation path via PyO3.
- Basic migration generation, dry-run, and apply workflow.
- Query timing and error classification hooks.
- Documentation for positioning, quickstart, and core API.

Outcome:

- Prove that Ferrum can deliver a productive async ORM path for a simple production-shaped service.

### v0.2 - Application Completeness

Scope:

- Relationship support for common one-to-many and many-to-one patterns.
- Transaction helper API.
- Bulk operations.
- Query optimization improvements.
- Expanded examples and framework integration guidance.

Outcome:

- Make Ferrum viable for more realistic application domains without broadening database scope.

### v0.3 - Migration And Tooling Maturity

Scope:

- Schema diff engine improvements.
- CLI tooling.
- Migration history inspection.
- Safer migration diagnostics.
- Developer workflow polish.

Outcome:

- Make schema evolution trustworthy enough for teams adopting Ferrum across multiple services.

### v1.0 - Production Stability

Scope:

- Stability guarantees for public APIs.
- Advanced relationships.
- Performance benchmarking and regression gates.
- Complete documentation.
- Production readiness guidance for observability, concurrency, transactions, and migrations.

Outcome:

- Establish Ferrum as a dependable ORM choice for async Python teams running PostgreSQL-backed services in production.

## Dependencies

- Chief Architect feasibility review for the Rust/PyO3 engine boundary, concurrency model, migration architecture, and PostgreSQL connection strategy.
- Product Designer review for documentation information architecture, onboarding flow, and developer experience touchpoints.
- Security Engineer review before implementation touches credential handling, database URLs, query logging, migration logging, or examples that might include secrets.
- Engineering input on benchmark targets, supported Pydantic v2 field subset, and migration safety guarantees.

## Risks And Tradeoffs

- Narrow PostgreSQL-first scope improves correctness but delays teams that require SQLite or MySQL.
- Async-only positioning is differentiating but excludes teams that still need synchronous scripts or Django-native integration.
- Rust/PyO3 engine requirements may improve performance but add packaging, build, debugging, and concurrency complexity.
- Django-inspired API can accelerate adoption, but overpromising Django compatibility would create support risk.
- Migration support in v0.1 is valuable, but unsafe migration behavior would harm trust more than a narrower supported subset.
- Observability hooks add early product value, but overly broad telemetry commitments could slow the foundation release.

## Open Questions

- What exact Pydantic v2 field types are Must-have for v0.1?
- What benchmark workloads and baseline thresholds define acceptable v0.1 performance?
- Should v0.1 include an explicit transaction helper, or defer transaction ergonomics to v0.2?
- What migration operations are safe enough for v0.1 apply support versus dry-run-only output?
- What packaging targets are required for the Rust/PyO3 engine in the first release?

## Scope Exclusions Summary

- No sync ORM API.
- No SQLite.
- No multi-database support.
- No full Django compatibility.
- No admin interface.
- No complex relationships in v0.1.
- No architecture decisions beyond product constraints stated in this document.

## Handoff Notes

Product requirements are ready for design review. The Product Designer should use this document to define the developer onboarding narrative, documentation structure, and first-run workflow. After design review, the Chief Architect should review technical feasibility and convert the accepted product scope into architecture options.
# Ferrum Product Requirements

## Vision

Ferrum is an async-first ORM for modern Python services that combines a Django-inspired developer experience, Pydantic v2-native models, and a Rust-powered SQL engine for PostgreSQL.

Ferrum exists because backend teams building with FastAPI, Starlette, and other async Python frameworks often have to choose between ergonomic ORM workflows, reliable async support, strong typing, and runtime performance. Ferrum's product bet is that those tradeoffs should not be forced for PostgreSQL-backed services.

Ferrum is differentiated by:

- Async-first behavior with no synchronous compatibility layer in the product contract.
- Pydantic v2-native models so application validation and persistence models do not diverge.
- A familiar Django-inspired QuerySet and migration experience for Python developers who value productive defaults.
- A Rust-powered SQL and hydration engine, exposed through Python via PyO3, to keep hot paths fast without leaking implementation complexity into the Python API.
- PostgreSQL-first scope that optimizes deeply for one production database before expanding.

## Problem Statement

Async Python teams need a persistence layer that feels productive, type-aware, and production-ready without requiring them to abandon async-native service patterns or duplicate schemas across validation and database layers.

Current alternatives leave gaps:

- Django ORM provides a proven developer experience but is not async-first and is coupled to Django's application model.
- SQLAlchemy is powerful and mature but can be verbose for teams seeking Django-style productivity and Pydantic-native model definitions.
- Tortoise ORM is async-friendly but does not establish the same product promise around Pydantic v2-native modeling, Rust-backed performance, and Django-inspired migrations.

Using the Jobs-to-be-Done lens, Ferrum is hired to let async Python teams define data models once, query PostgreSQL ergonomically, and move from prototype to production without switching persistence tools.

## Target Audience

### Primary Persona: Async Python Backend Developer

Builds FastAPI, Starlette, Litestar, or similar services and wants database access that matches async application code. Values clear APIs, type hints, predictable behavior, and integration with existing Python tooling.

### Secondary Persona: Django ORM Migrator

Has experience with Django models, QuerySets, and migrations, but is building a standalone async service. Wants familiar product semantics without adopting the full Django framework.

### Secondary Persona: Backend Team Lead

Owns service reliability and delivery speed. Needs an ORM that improves developer throughput while preserving performance, observability, and migration safety.

## Product Goals for v0.1

Ferrum v0.1 should prove the core product loop: define a Pydantic v2 model, persist it to PostgreSQL, query it asynchronously through a Django-inspired API, and understand failures well enough to debug development usage.

Using MoSCoW:

- Must-have: async CRUD for PostgreSQL-backed Pydantic v2 models.
- Must-have: Django-inspired QuerySet API for common filters, ordering, limits, and retrieval.
- Must-have: generated SQL behavior that is inspectable enough for developers to debug.
- Must-have: a minimal migration workflow that can create and evolve schemas during early development.
- Should-have: type-safe filter ergonomics that catch common invalid field usage before runtime where practical.
- Should-have: basic observability hooks for query timing, SQL visibility, and error context.
- Could-have: benchmark comparisons against common ORM paths.
- Won't-have in v0.1: sync API support, multi-database support, SQLite support, advanced relationship loading, and production-grade migration branching.

The outcome-over-output success target is not "ship an ORM package"; it is that a developer can build a small async PostgreSQL service with Ferrum and avoid replacing the persistence layer after the prototype.

## Non-Goals

Ferrum v0.1 explicitly does not include:

- Synchronous ORM support or sync compatibility wrappers.
- Multi-database support beyond PostgreSQL.
- SQLite support, including local-only development mode.
- MySQL, MariaDB, SQL Server, or non-relational databases.
- Full Django compatibility or a drop-in replacement for Django ORM.
- A web framework, dependency injection system, admin UI, or authentication layer.
- Advanced relationship loading such as deep eager loading, polymorphic relations, or complex prefetch behavior.
- Distributed transactions, sharding, read replicas, or multi-tenant routing.
- A public stability guarantee for the Python API before v1.0.

These exclusions are intentional ruthless prioritization: delaying broad database coverage and sync compatibility protects the async PostgreSQL product loop that makes Ferrum valuable.

## Core Features

### Async QuerySet API

Developers can call `await Model.objects...` for common read and write operations using chainable QuerySet semantics inspired by Django.

Required v0.1 capabilities:

- Create, get, filter, update, and delete records asynchronously.
- Chain filters, ordering, limits, and offsets for common list screens and API endpoints.
- Return typed Pydantic model instances from query results.
- Surface query errors with model, operation, and SQL context.

### Pydantic v2-Native Models

Ferrum models are defined using Pydantic v2-compatible field semantics so validation, serialization, and persistence share one model definition.

Required v0.1 capabilities:

- Define persisted fields with Python type annotations.
- Support defaults and nullable fields where the database schema can represent them.
- Preserve Pydantic validation on model construction and result hydration.
- Document unsupported Pydantic field patterns clearly.

### Rust-Powered SQL Engine via PyO3

Ferrum uses a Rust engine for SQL generation, query planning primitives, and result hydration hot paths while keeping a Python-native API.

Required v0.1 capabilities:

- Convert supported QuerySet operations into PostgreSQL SQL and parameters.
- Avoid string-concatenated user input in generated SQL.
- Return structured errors that Python callers can catch and log.
- Keep Rust internals behind stable Python-facing product behavior.

### Django-Inspired Migrations

Ferrum provides a migration workflow familiar to Django users while remaining scoped to PostgreSQL and Ferrum models.

Required v0.1 capabilities:

- Detect model/schema changes for supported field types.
- Generate migration files or equivalent durable migration artifacts.
- Apply migrations asynchronously or through a CLI command suitable for development.
- Show clear messages when a schema change is unsupported or unsafe.

### PostgreSQL-First Connection and Execution

Ferrum supports PostgreSQL as the only v0.1 database target.

Required v0.1 capabilities:

- Configure database connection settings through explicit Python configuration or environment-backed configuration.
- Execute queries safely under async application workloads.
- Expose predictable behavior for connection failures, query timeouts, and transaction errors.

### Observability and Debuggability

Ferrum must make common development and production failure modes diagnosable.

Required v0.1 capabilities:

- Include structured error messages for validation, connection, SQL generation, and database execution failures.
- Provide a documented way to inspect generated SQL and query timings.
- Avoid exposing sensitive parameter values in default logs.

## User Stories and Acceptance Criteria

### Model Definition

As an async Python developer, I want to define a Ferrum model with Pydantic v2-compatible type annotations, so that validation and persistence use the same source of truth.

Acceptance criteria:

- Given a model with supported primitive fields, Ferrum can derive the corresponding persisted schema metadata.
- Given defaults or nullable fields, Ferrum documents and applies deterministic database behavior.
- Given an unsupported field type, Ferrum returns a clear product-level error instead of silently ignoring the field.

### Create and Retrieve Records

As an API developer, I want to create and retrieve PostgreSQL records asynchronously, so that request handlers can use Ferrum without blocking the event loop.

Acceptance criteria:

- `await Model.objects.create(...)` persists a valid model and returns a typed model instance.
- `await Model.objects.get(...)` returns one typed model instance when exactly one record matches.
- Missing or multiple-result cases have documented, catchable error behavior.

### Query Lists

As a backend developer building API endpoints, I want a chainable QuerySet API for filters, ordering, and limits, so that common list endpoints are concise and readable.

Acceptance criteria:

- Developers can chain supported filters, `order_by`, `limit`, and `offset`.
- The final async execution method returns typed Pydantic model instances.
- Generated SQL is parameterized and inspectable for debugging.

### Update and Delete Records

As a service developer, I want to update and delete records through the async ORM API, so that basic lifecycle operations do not require raw SQL.

Acceptance criteria:

- Supported update operations return documented result counts or typed instances.
- Delete operations return documented result counts or completion signals.
- Validation and database constraint failures surface as catchable Ferrum errors.

### Development Migrations

As a Django-experienced developer, I want Ferrum to generate and apply migrations from model changes, so that schema evolution is part of the normal development workflow.

Acceptance criteria:

- Ferrum detects additions, removals, and type changes for supported fields.
- Ferrum produces durable migration artifacts or commands that can be reviewed before applying.
- Unsupported or destructive changes require explicit developer action and clear messaging.

### Debug Failed Queries

As a team lead responsible for production readiness, I want query errors and slow queries to include actionable context, so that failures can be diagnosed without guessing.

Acceptance criteria:

- Query execution errors include model name, operation type, and safe SQL context.
- Default logs do not expose sensitive parameter values.
- A documented debug mode or hook can expose expanded SQL/query timing details for local investigation.

## Prioritization

Using the Kano Model:

- Threshold requirements: async PostgreSQL CRUD, Pydantic v2 model definitions, parameterized SQL, clear errors.
- Performance requirements: Rust-backed SQL generation and hydration, query timing visibility, predictable async execution behavior.
- Delighters: Django-like ergonomics in standalone async services, migration workflow that feels familiar without requiring Django.

The v0.1 scope prioritizes threshold requirements first. Performance work matters where it proves the Rust-powered product thesis, but not at the expense of a coherent developer workflow.

## Success Criteria

Ferrum v0.1 is successful when:

- A developer can build a small FastAPI or Starlette service using Ferrum for PostgreSQL-backed CRUD without writing raw SQL for standard operations.
- A developer can define persisted models using Pydantic v2-compatible annotations without duplicating schema definitions.
- Common QuerySet flows are documented and work asynchronously end to end.
- Supported schema changes can be converted into reviewable migrations.
- Generated SQL can be inspected during development without exposing sensitive values by default.
- Early users can identify whether Ferrum is a fit within 30 minutes of reading docs and running a quickstart.

Suggested product metrics:

- Time-to-first-successful-query for a new developer is under 30 minutes.
- Quickstart completion rate is at least 70% among invited alpha testers.
- At least 80% of alpha tester CRUD use cases avoid raw SQL for supported models.
- Fewer than 20% of alpha feedback items are caused by unclear error messages or missing scope documentation.
- No known default logging path exposes sensitive query parameter values.

## Roadmap

### v0.1: Core Product Loop

High-level scope:

- PostgreSQL-only async connection and execution.
- Pydantic v2-native model definitions.
- Basic CRUD and QuerySet read flows.
- Parameterized SQL generation through the Rust engine.
- Minimal migration generation and apply workflow for supported fields.
- Basic observability and safe debug surfaces.

Exit criteria:

- A small async API service can use Ferrum for model definition, CRUD, queries, and development migrations.

### v0.2: Relationship and Transaction Depth

High-level scope:

- First-class relationships for common one-to-many and many-to-one use cases.
- Explicit transaction APIs.
- Bulk operations.
- Query optimization for common relationship and bulk paths.
- Expanded migration safety checks.

Exit criteria:

- Ferrum supports realistic service data models beyond single-table CRUD without requiring raw SQL for common relationship paths.

### v0.3: Tooling and Production Hardening

High-level scope:

- More complete CLI workflow.
- Schema diff reliability improvements.
- Expanded observability integrations.
- Performance benchmarks and regression tracking.
- Documentation for production deployment patterns.

Exit criteria:

- Teams can evaluate Ferrum against existing ORMs using repeatable benchmarks, docs, and operational guidance.

### v1.0: Stable Public Release

High-level scope:

- Stable Python API contract.
- Production-ready migration guarantees for supported patterns.
- Advanced relationship support.
- Complete documentation and upgrade guidance.
- Defined compatibility policy.

Exit criteria:

- Ferrum is suitable for production adoption by async Python teams that accept PostgreSQL-first scope.

## Dependencies

- Product design input for documentation structure, onboarding flow, and first-run developer experience.
- Architecture review for Rust/Python boundary feasibility, migration safety, concurrency behavior, and PostgreSQL execution model.
- Security review for SQL generation, parameter handling, logging defaults, and PII-safe observability.
- Engineering validation for Pydantic v2 compatibility constraints and PyO3 packaging expectations.

## Risks and Failure Modes

- Scope risk: adding sync or multi-database support too early would dilute Ferrum's differentiator and slow the v0.1 loop.
- Concurrency risk: async APIs must avoid hidden blocking behavior, unsafe shared state, and unclear transaction boundaries.
- Performance risk: Rust adds product value only if hot paths are measurably faster or more predictable than pure Python alternatives.
- Packaging risk: PyO3 distribution friction can undermine time-to-first-success if installation is difficult.
- Migration risk: unsafe schema diffs can cause data loss if destructive operations are too implicit.
- Observability risk: exposing raw SQL parameters in logs could leak sensitive data.
- Adoption risk: Django-inspired semantics must be familiar without implying full Django compatibility.

## Out-of-Scope Decisions for Architecture

This PRD intentionally does not decide:

- Rust crate boundaries or internal module structure.
- Python package layout.
- Specific async PostgreSQL driver choice.
- Migration file format.
- Connection pooling implementation.
- Benchmark harness design.

Those belong to the Chief Architect and engineering owners after product requirements are accepted.
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
