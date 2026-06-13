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
- **Secure defaults.** Ferrum must default to injection resistance, credential redaction, production-safe diagnostics, and explicit gates for destructive operations.

## Goals for v0.1 (MoSCoW)

### Must-have

- Async PostgreSQL CRUD using a QuerySet-style API.
- Pydantic v2 model definitions that drive schema and validation.
- Rust/PyO3-powered SQL generation that emits parameterized queries.
- Migration plan generation, dry-run output, destructive-operation gates, and apply workflow for supported schema changes.
- Query observability hooks that capture duration, safe SQL context, and failure classification.
- Security and data protection requirements for SQL compilation, credential redaction, observability defaults, error handling, and migration safety.
- Documentation that compares Ferrum to SQLAlchemy, Tortoise ORM, and Django ORM.

### Should-have

- Common filter operators (exact, contains, comparisons, null checks, boolean logic).
- Clear error messages with field/query/model context without leaking secrets.
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
- Raw SQL passthrough, user-supplied SQL templates, string fragments, or `extra()`-style escape hatches.

## Non-Goals

Ferrum v0.1 explicitly avoids:

- Sync engines or blocking compatibility layers.
- Multi-database targeting (PostgreSQL only).
- SQLite or framework-specific adapters.
- Replacing full web frameworks, admin UIs, or auth stacks.
- Deep relationship loading, polymorphic relations, or automatic prefetch planning.
- Raw SQL escape hatches or production-exposed query inspection surfaces.

## Security & Data Protection Requirements

Ferrum v0.1 treats query compilation, observability payloads, error surfaces, and migration execution as product-level security scope. The requirements below avoid prescribing architecture, but they must be testable in release qualification before implementation is considered complete.

### SQL Compilation Safety

Product requirements:

- SQL identifiers, including tables, columns, operators, and sort directions, resolve only from model metadata allowlists.
- Runtime user strings are never concatenated into SQL identifier positions.
- Query values are emitted only as bound parameters.
- Filter operators use an explicit supported operator set; unsupported operators fail before SQL is emitted.
- Raw SQL passthrough, string fragments, user-supplied SQL templates, and `extra()`-style escape hatches are out of scope for v0.1.

Acceptance criteria:

- Unknown fields, unsupported operators, and invalid sort directions raise structured compilation errors before database execution.
- Supported query paths emit parameterized SQL plus bound parameter metadata, not interpolated user values.
- Release qualification includes automated coverage proving user input is not interpolated into SQL across supported create, filter, order, update, delete, and migration compilation paths.
- Documentation states that any future raw SQL surface requires a separate product and security review before implementation.

### Credential Handling

Product requirements:

- Connection strings, passwords, and secret-bearing config values must not appear in default hook payloads, exceptions, migration dry-run output, or migration apply output.
- Connection diagnostics may include host, port, database name, username, and error category, but never password or full DSN.
- Documentation must warn application teams not to log full DSNs or secret-bearing configuration around Ferrum setup.

Acceptance criteria:

- A fixture DSN with a known username, password, host, database, and query parameters is absent from default errors, hooks, dry-run output, and apply output except for allowlisted non-secret fields.
- Connection error payloads match a documented allowlist and fail tests if password or full DSN substrings appear.
- Migration dry-run and apply output are scanned during release qualification for DSN and password patterns.

### Observability Defaults

Product requirements:

- SQL context is defined as parameterized query template, operation name, duration, parameter count, parameter type summary, and failure category.
- Production-default SQL context is Tier A only: query fingerprint, model/operation metadata, duration, status, and failure category.
- Bound parameter values must not appear in default hook payloads under any key.
- Production-safe defaults expose minimal diagnostic context; Tier B normalized SQL and Tier C full SQL/bind diagnostics require Ferrum-specific opt-in and are documented as unsafe for production when they can expose sensitive values.
- Hook integration documentation must warn against logging bound parameters, DSNs, hydrated model bodies, or submitted validation values.

Acceptance criteria:

- Default query start, success, failure, and migration hook payload schemas exclude raw parameter values and full connection strings.
- Default validation errors include field names and constraint failures without echoing submitted values.
- Documented diagnostics tiers exist for default production use and opt-in development debugging, with bound-value logging never enabled by default or generic `DEBUG=1`.
- Observability examples demonstrate safe payloads using synthetic data only.

### Error Handling and Debug Boundaries

Product requirements:

- Database execution errors map to stable, sanitized Ferrum error types before surfacing to Python callers.
- Raw PostgreSQL `DETAIL` and `HINT` values that contain row data or submitted values are not exposed by default.
- Verbose diagnostic fields, including compiled SQL text and internal operation names, require explicit debug configuration.
- Rust panics and internal PyO3 boundary failures map to catchable Python exceptions in normal operation without memory addresses, local file paths, or process aborts.
- Documentation must warn that `get()` not-found and multiple-result semantics can act as existence oracles; authorization remains an application-layer responsibility before query execution.

Acceptance criteria:

- Duplicate key, foreign key, timeout, cancellation, and connection failures produce deterministic Ferrum error categories.
- PostgreSQL errors containing row values are converted to generic production-safe messages by default.
- Debug-enabled error payloads are opt-in and covered by tests that verify default production-safe payloads remain sanitized.
- PyO3 panic/error-boundary tests verify callers receive catchable Python exceptions rather than leaked internals or uncontrolled aborts.

### Migration Safety

Product requirements:

- Migration dry-run output is mandatory for v0.1 migration workflows and must be available before apply.
- Destructive migration actions require explicit confirmation before apply. Destructive actions include column drop, table drop, type narrowing, and adding `NOT NULL` to populated columns.
- Applying migrations against non-development targets requires explicit environment confirmation.
- Each migration step must document atomicity expectations and recovery guidance for partial failure states.
- Unscoped bulk `delete()` or `update()` operations require an explicit danger API rather than default terminal operations.

Acceptance criteria:

- Destructive migrations fail to apply unless dry-run review and explicit destructive confirmation are present.
- Type narrowing is classified as destructive and follows the same confirmation path as drops.
- Non-development migration apply fails without explicit environment confirmation.
- Migration failure output states whether the database changed, which step failed, and the documented recovery action.
- Calling `delete()` or `update()` without filters fails unless the caller uses the named danger API documented for all-record mutations.

## Core Features

### Async QuerySet and CRUD

Ferrum must expose an awaitable QuerySet-style API for create, read, update, delete, filter, order, limit, and retrieve operations.

Product requirements:

- Terminal operations (`create`, `get`, `first`, `all`, `count`, `update`, `delete`) execute only when awaited.
- Chain methods (`filter`, `exclude`, `order_by`, `limit`, `offset`) are composable without hitting the database.
- No synchronous alias or blocking helper is introduced in v0.1.
- Unscoped bulk mutation requires an explicit named danger API.

Acceptance criteria:

- Developers can `await Model.objects.create(...)` to insert and hydrate a typed model.
- Queries with filters, ordering, and limits execute when awaited and return typed results.
- Unsupported fields/operators raise actionable errors before or during SQL compilation.
- Execution failures surface model/query context without leaking raw row data.
- Default `update()` and `delete()` calls without filters fail unless the named danger API is used.

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
- SQL identifiers resolve from model metadata allowlists rather than runtime string concatenation.
- Filter operators and sort directions are enum-validated before SQL generation.
- Structured compilation errors bubble up as understandable Python exceptions.
- Performance trade-offs remain transparent; opaque failures are unacceptable.

Acceptance criteria:

- Supported queries emit parameterized SQL and bound parameter lists.
- Unsupported query shapes raise structured errors rather than malformed SQL.
- Runtime field names, operator strings, and sort directions outside model metadata allowlists fail before SQL emission.
- Release tests prove untrusted input is never interpolated into SQL text across supported query shapes.
- Concurrent compilation reuses immutable state and never mutates shared request data.

### Django-Inspired Migrations

Ferrum provides a migration workflow inspired by Django while remaining framework-neutral.

Product requirements:

- Generate migration plans from model schema diffs.
- Expose dry-run output showing SQL before applying.
- Apply migrations to PostgreSQL via documented commands or API with safety guards.
- Require explicit confirmation for destructive actions and non-development targets.
- Classify field removal, table removal, narrowing type changes, and populated `NOT NULL` additions as destructive.
- Document per-step atomicity expectations and recovery actions for partial failure states.
- Migration results are deterministic for the same schema state.

Acceptance criteria:

- New models produce migrations that create expected PostgreSQL tables.
- Field additions/removals/types generate describable schema changes.
- Dry-run requests return the planned SQL without mutating the database.
- Destructive migration apply requires explicit confirmation and non-development apply requires explicit environment confirmation.
- Unsafe or unsupported migration actions fail with explicit guidance.

### Observability & Failure Visibility

Ferrum must make production failure modes diagnosable and avoid leaking sensitive data.

Product requirements:

- Hooks/events for query start, success, failure, SQL inspection, and hydration failure.
- Default logging avoids raw bound parameter values.
- Default hook payloads exclude full DSNs, credentials, bound values, and hydrated model bodies.
- SQL context is tiered so production defaults stay minimal and verbose diagnostics require explicit opt-in.
- Documentation covers failure categories for validation, compilation, connection, execution, and migrations.
- Concurrency expectations for async queries are explicit so developers know what can be shared.

#### Security & Data-Handling Constraints

Ferrum v0.1 observability must default to production-safe metadata and make higher-risk diagnostics explicit, local, and opt-in.

Product requirements:

- SQL context has three product tiers:
  - Tier A (default and safe for production/APM): query fingerprint, operation name, model/table/field identifiers from Ferrum metadata, duration, status, and failure category.
  - Tier B (explicit staging/dev opt-in): normalized SQL text and migration dry-run SQL with placeholders intact.
  - Tier C (explicit unsafe local-dev opt-in): full compiled SQL and bound parameter values for query inspection only; Tier C data is never safe for APM, centralized logs, production defaults, or migration output.
- Tier B and Tier C require Ferrum-specific configuration and must never activate from a generic `DEBUG=1` setting alone.
- Redacted values use placeholders such as `[REDACTED]` or typed placeholders, with no string/byte prefix, suffix, or length hints.
- Bound parameter values, connection strings, passwords, secrets, hydrated model bodies, and raw row data are excluded from default hook payloads, exceptions, logs, and migration dry-run output.
- Migration dry-run output shows reviewable SQL and operation metadata, but redacts credentials and never embeds bind values.
- Error messages may include field names, model names, operation names, constraints, and failure categories; submitted field values are omitted by default.
- Hook payloads use an allowlist contract that marks each attribute as safe-for-APM or dev-only. Dev-only attributes require explicit opt-in and local execution context.
- Driver and PyO3 boundary errors exposed through Ferrum hooks, logs, or exceptions are sanitized before reaching user-facing messages.
- Query inspection UX for v0.1 is local-dev only and must not expose a production HTTP route, hosted dashboard, or shared browser view containing full SQL or bound values.

Acceptance criteria:

- Developers can record duration and context for each query.
- Failure classification distinguishes validation, compilation, and execution issues.
- Concurrent awaits on independent queries do not mutate shared compiled state.
- Migration failure messages specify whether the database changed before failure.
- Default hook payloads include Tier A metadata only and do not contain raw bound values under any key.
- Opting into full parameterized SQL requires explicit local-dev configuration and remains separate from production/APM hook payloads.
- Validation and execution errors identify fields or constraints without echoing submitted values by default.
- Migration dry-run output can be reviewed without exposing bind values, passwords, or full DSNs.
- Collection and JSON values in hooks/errors are capped or redacted so size and nested keys do not become sensitive-data side channels.

### PostgreSQL-First Connectivity & Execution

Ferrum throttles scope to PostgreSQL in v0.1 to guarantee correctness and clarity.

Product requirements:

- Connection settings documented for local and deployed environments.
- Query execution handles PostgreSQL-specific behaviors (timeouts, cancellations).
- Connection failures surface actionable errors without exposing credentials.
- Database execution errors map to sanitized Ferrum error types before surfacing to callers.
- Validation and execution errors avoid submitted value echo by default.
- No SQLite shortcuts or multi-database fallbacks appear in user-facing docs.

Acceptance criteria:

- Connection configuration works via explicit Python config or env vars.
- Timeouts, cancellations, and connection drops emit deterministically classified errors.
- Observability hooks document PostgreSQL behavior for deployments.
- Connection failures expose allowlisted diagnostics only and never include passwords or full DSNs.
- PostgreSQL `DETAIL` or `HINT` data values are not exposed by default in Ferrum exceptions.
- Rust/PyO3 internal failures map to catchable Python exceptions without memory addresses, local file paths, or process abort in normal operation.

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
- Unscoped destructive operations require explicit danger APIs and fail by default.

### Query Composition

As a backend developer, I want to compose filters, ordering, and limits through a QuerySet-like API, so that common queries feel readable.

Acceptance criteria:

- Chains support the documented operator subset.
- Ordering handles ascending/descending across supported fields.
- Limit and offset push down predictably.
- Invalid filters fail before hitting SQL.
- Unknown fields, unsupported operators, and invalid sort directions are rejected from metadata allowlists before SQL generation.

### Migration Planning

As a Django-experienced developer, I want schema changes to produce reviewable migration plans, so that database changes can be inspected safely.

Acceptance criteria:

- Migration generation detects supported model diffs.
- Dry-run output lists operations and SQL.
- Apply commands record enough state to avoid double-applying.
- Unsupported schema changes fail with next-action guidance.
- Destructive operations and non-development applies require explicit confirmation gates.

### Observability

As an operator, I want query timing and errors to be observable, so that production issues can be triaged without exposing sensitive data.

Acceptance criteria:

- Hooks describe query start, success, failure, and safe SQL context.
- Hook payloads default to Tier A metadata and exclude raw parameter values, credentials, full DSNs, hydrated model bodies, and raw row data.
- Full SQL inspection is explicit local-dev opt-in; bound-value inspection is unsafe local-dev only and never safe-for-APM.
- Field and constraint names may appear in errors; submitted values do not appear by default.
- Error categories differentiate validation, compilation, connection, execution, and migration failures.

## Success Criteria

- A new developer can go from Ferrum documentation to a running FastAPI/Starlette prototype with async PostgreSQL CRUD in under 30 minutes.
- The v0.1 API supports the CRUD, filter, order, limit, and migration paths needed by a simple service.
- Documentation clearly describes when to choose Ferrum over SQLAlchemy, Tortoise ORM, or Django ORM.
- Benchmarks cover query compilation, execution, and hydration baselines without regressions during release qualification.
- Validation, query compilation, and migration planning errors are actionable without referencing Ferrum source code.
- Observability hooks supply duration, Tier A SQL context, and failure category in logs or metrics without exposing bound values, user-submitted values, or credentials.
- Security release qualification proves identifier allowlisting, no default credential or PII leakage, sanitized PostgreSQL errors, destructive migration gates, and unscoped bulk mutation safeguards.
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
- Security/data-protection requirements for SQL compilation, observability, errors, credentials, and migration safety.
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
- Expanded migration diagnostics beyond the mandatory v0.1 destructive-operation gates.
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
- Security Engineer re-review of this amended PRD before architecture sign-off, especially query inspection, observability payloads, and migration output.
- Engineering input on benchmark targets, supported Pydantic v2 subset, and migration safety guarantees.

## Risks & Tradeoffs

- PostgreSQL-first focus improves correctness but delays SQLite or MySQL teams.
- Async-only positioning differentiates but excludes sync workloads and Django-native scripts.
- Rust/PyO3 tooling adds packaging, build, and debugging complexity.
- Django-inspired semantics risk overpromising compatibility.
- Migration support must be conservative; unsafe behavior damages trust faster than a narrower subset.
- Observability hooks add early value but can slow the foundation release if scope grows.
- Secure observability defaults reduce data-exposure risk but defer rich SQL and OTel inspection to explicit dev/staging paths.
- Security gates reduce v0.1 flexibility, especially raw SQL and destructive migration flows, but protect the product's core job: safe async database access.

## Product Decisions and Open Questions

Resolved for v0.1:

- Migration command set: v0.1 must support project initialization, migration generation, mandatory dry-run/preview, and guarded apply. Rollback is not a v0.1 Must-have; recovery guidance can use forward migrations and documented manual intervention for early releases.
- Relationship scope: no relationship helper is strategically required for the v0.1 activation path. v0.1 examples should use scalar fields and explicit foreign-key IDs only. Common one-to-many and many-to-one helpers move to v0.2.
- Observability scope: hooks must include query start, success, failure, SQL context, hydration failure, and migration diagnostics with Tier A safe-for-APM defaults. Full SQL inspection is explicit local-dev opt-in and bound-value inspection is unsafe local-dev only.
- Positioning: Ferrum should be positioned as a general async Python ORM with FastAPI and Starlette as the primary activation examples, and Django migration as a secondary adoption story.

Still open for architecture:

- What packaging targets must the Rust/PyO3 engine support in the first release?

## Scope Exclusions Summary

- No synchronous ORM APIs or wrappers in v0.1.
- No SQLite, MySQL, or multi-database support.
- No admin interface, advanced relationships, or Django framework dependency.
- No raw SQL escape hatches or production-exposed query inspection surfaces.
- No architecture decisions beyond the product constraints documented here.

## Handoff Notes

Product requirements are ready for Product Designer review to define the documentation/onboarding story, developer-local query inspection UX, safe SQL-context language, and migration preview experience. After design sign-off and Security Engineer re-review, Chief Architect should validate the Rust/PyO3 boundary, async concurrency guarantees, SQL compilation safety model, centralized error sanitization, hook payload schema, and migration tooling before engineering ramps implementation.
