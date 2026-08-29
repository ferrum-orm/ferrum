# Changelog

All notable changes to Ferrum are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Ferrum versions as 0.x under [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
with SemVer's unstable-major rule: **0.x has no compatibility guarantee**. MINOR and
PATCH may both break. After the first 1.0 release, removing or breaking a documented
public Python API or the migration file/ops format requires a deprecation window of at
least one minor release and 90 days, whichever is longer, recorded here and in
`README.md`. Generated SQL is never a compatibility surface.

---

## [Unreleased]

### Added

- Live multi-driver integration coverage for PostgreSQL, MySQL, SQLite, and SQL
  Server, with PostgreSQL + SQLite required on pull requests and an independent
  four-backend nightly matrix.

### Changed

- Local database tooling now starts health-checked PostgreSQL, MySQL, and SQL
  Server test services with the same `ferrum_test` credentials used by CI.
- Backend support is documented and tested as an explicit capability matrix
  matching the integration registry: PostgreSQL has full coverage; MySQL and
  SQL Server claim FTS and composite primary keys only; SQLite additionally
  claims `RETURNING`. Transactions, aggregates, and upsert are PostgreSQL-only.
- Compatibility policy: 0.x carries no guarantee (MINOR and PATCH may both break);
  1.0 starts a deprecation window of one minor + 90 days for public API and
  migration-ops. Generated SQL remains a non-compatibility surface.

### Fixed

- Upsert attempts on MySQL, SQLite, and SQL Server now fail early with a typed
  Ferrum configuration error instead of sending PostgreSQL `ON CONFLICT` SQL to
  an unsupported backend.
- SQLite now drains `RETURNING` cursors before commit, cleans up cancelled
  operations, and hydrates integer-backed boolean columns as `bool`. Relation
  prefetch SQL now uses each driver's identifier quoting and parameter style
  instead of PostgreSQL-only syntax.

---

## [0.1.17] - 2026-08-10

### Added

### Changed

### Fixed

- Vector writes now bind a pgvector text literal instead of a raw `list[float]`.
  asyncpg has no codec for the `vector` type and falls back to text, so
  `create()`, `update()`, `bulk_create()`, `bulk_update()`, and `upsert()`
  raised `FerrumDatabaseError: DataError` ("expected str, got list") on any
  `Vector` field. Read-path `nearest_to()` / `vector_search()` already encoded
  the literal; the write paths did not.
- `ferrum.ext.pgvector.register_vector_codecs()` now registers the `vector`
  codec pool-wide. asyncpg exposes `set_type_codec` on a connection rather than
  a pool, so the previous fallback configured only the one connection it
  acquired — every other pooled connection decoded `vector` columns as `str`,
  making a read's result depend on which connection served it. The asyncpg
  driver gained `add_type_codec()`, which applies codecs from the pool `init`
  hook and expires live connections so the registration is uniform. The
  `timeout` argument now actually bounds the `CREATE EXTENSION` statement
  (it was previously assembled into an unused string).

---

## [0.1.16] - 2026-08-09

### Added

### Changed

### Fixed

- `bulk_update()` now casts each `VALUES` placeholder to its DDL column type
  instead of a lossy approximation. A `uuid` primary key compiled to
  `$1::text`, so the `t.pk = v.pk` join raised
  `FerrumDatabaseError: UndefinedFunctionError` (PostgreSQL 42883); `uuid[]`
  and `tsvector` columns failed the `SET` assignment, and `numeric` columns
  failed parameter binding against a `double precision` cast.

---

## [0.1.15] - 2026-07-26

### Added

### Changed

### Fixed

---

## [0.1.14] - 2026-07-26

### Added

### Changed

### Fixed

---

## [0.1.13] - 2026-07-26

### Added

- Typed `QuerySet` aggregates (`count`/`sum`/`avg`/`min`/`max`) with grouping,
  filtered aggregates, date buckets, bound `HAVING`, and structured dict rows.
- Filtered `update_returning()` for atomic compare-and-set claims.
- Async context-managed `QuerySet.stream()` with bounded PostgreSQL chunks,
  normal projection/deferred materialization, and deterministic cursor cleanup.
- `Field(jsonb_list=True)`, generated/read-only field metadata, and read-only
  PostgreSQL schema-fidelity drift reporting.

### Changed

- Create/update/upsert/bulk writes now derive their assignment allowlist from
  `ModelMetadata.writable_fields`; generated/read-only columns remain selectable
  and hydratable but cannot be written.

### Fixed

---

## [0.1.12] - 2026-07-26

### Fixed

- **Inherited relation descriptors**: `ClassVar` `ForeignKey` / `OneToOne` /
  `ManyToMany` declared on a parent model are now collected via MRO, so
  subclasses (e.g. `Ticket(TicketRead)` with `team` on `TicketRead`) support
  `filter(team__slug=...)` and `select_related("team")`.

---

## [0.1.11] - 2026-07-26

### Added

- **Relation-filter JOINs**: Django-style one-level lookups
  (`filter(team__slug=...)`, `Q(team__slug=...) | Q(team__id=...)`) auto-INNER-JOIN
  the related FK/OTO table. Combines with `select_related()` (reuses LEFT JOIN).
  Nested hops and relation lookups on UPDATE/DELETE are rejected.

### Changed

### Fixed

---

## [0.1.10] - 2026-07-26

### Added

- **`QuerySet.project(model)`**: hydrate rows into a different model that maps to
  the same table (e.g. `Ticket.objects.nearest_to(...).project(TicketRead)`).
  SELECT is restricted to shared fields; filters / KNN compile against the source.
- **SQL echo / verbose mode** (SQLAlchemy-like): `ferrum.enable_echo()`,
  `ferrum.connect(..., echo=True|"debug")`, or `FERRUM_ECHO=1|debug`. Prints
  compiled SQL (+ param types; bound values only in debug/verbose). Generic
  `DEBUG=1` never enables echo.

### Changed

- **`nearest_to()` + `order_by()`**: KNN distance is the primary `ORDER BY` key;
  plain `order_by()` columns are secondary tie-breakers (previously `order_by`
  silently dropped `vector_order_by`).
- **`nearest_to()` bind format**: query vectors are bound as pgvector text
  literals with an SQL `$N::vector` cast, fixing asyncpg `float[]` → `DataError`
  against pgvector columns.

### Fixed

---

## [0.1.9] - 2026-07-12

### Added

### Changed

### Fixed

---

## [0.1.8] - 2026-07-10

### Added

### Changed

### Fixed

---

## [0.2.0] - 2026-07-10

### Added

- **Instance input for `create()`**: `Model.objects.create(conn, instance_or_dict)`
  inserts a model instance (or dict) directly, mirroring `bulk_create()` semantics —
  values from `model_dump()`, auto-PK sentinel (`0`/`None`/`""`) dropped so the DB
  default runs. The kwargs form is unchanged. Mixing both forms raises
  `FerrumCompileError`.
- **`QuerySet.update_instance(conn, obj, *, fields=None)`**: persist one instance's
  field values to its row by primary key (composite PKs supported). Returns the
  affected row count; `0` signals a missing/stale row.

### Fixed

- `update()` / `delete()` now raise `FerrumCompileError` on a sliced queryset
  (`qs[:10]`) or one carrying `select_related()`/`nearest_to()`/`rank_by()` state.
  Previously `LIMIT`/`OFFSET` and join state were silently dropped from the write
  IR — `filter(...)[:10].delete()` deleted **all** matching rows.
- An insert whose row is empty after auto-PK sentinel dropping now fails with a
  structured `FerrumCompileError` before SQL emission instead of surfacing a raw
  PostgreSQL syntax error (`INSERT INTO t () VALUES ()`), for both `create()` and
  `bulk_create()`.
- Instance write paths (`create(conn, obj)`, `bulk_create`, `bulk_update`,
  `update_instance`) reject instances carrying deferred (`only()`/`defer()`)
  fields — `model_dump()` bypasses the deferred-field guard and would silently
  write class defaults for columns that were never loaded.

---

## [0.1.6] - 2026-07-01

### Fixed

- **inspectdb**: emit FK backing columns (`{name}_id`) and `ClassVar[ForeignKey]`
  relationship descriptors instead of invalid instance-field `ForeignKey`
  annotations that broke Pydantic model construction.

---

## [0.1.5] - 2026-07-01

### Added

- **Cross-dialect full-text search (ADR-007, IR v3)**: `match`, `match_phrase`,
  `match_websearch`, and `match_boolean` filter operators; `QuerySet.rank_by()` /
  `search()` for relevance ordering; `FullTextIndex` model declaration;
  `CreateFullTextIndex` / `DropFullTextIndex` / `CreateFullTextCatalog` migration
  ops; per-dialect emit in `ferrum-sql/src/fts/` and DDL in
  `ferrum.migrations.fts`; optional `ferrum.ext.fts.scored_search` helper.
- **`Field(fts_config=...)`** for PostgreSQL `regconfig` allowlisting on
  `TSVector` / indexed text fields.

### Changed

- **IR version 2 → 3** — adds optional `text_rank_by` node. Python `_IR_VERSION`
  and Rust `IR_VERSION` must stay synchronized.

### Fixed

- **inspectdb**: query `information_schema` via `driver.fetch()` instead of
  reaching into a private `_pool` on the timed query executor.

### Breaking

- **QuerySet IR version bump (2 → 3).** Any code that constructs or validates raw IR
  JSON (custom tooling, pinned compiler tests) must accept the new optional
  `text_rank_by` field. The public Python API is backward-compatible — existing
  filter-only FTS lookups continue to work; ranking is opt-in via `rank_by()` /
  `search()`.

---

## [0.1.4] - 2026-07-01

### Added

- **Examples**: `examples/sqlite/` (file-based SQLite, no Docker),
  `examples/mysql/` (`mysql://` + Docker Compose), and
  `examples/pyproject_config/` (`[ferrum]` in `pyproject.toml` with
  `database_url_env = "DATABASE_URL"`). Updated `examples/README.md` with a
  driver extras matrix.

### Changed

- `ferrum-migrate` crate version now follows `[workspace.package]` like the
  other workspace members.

### Fixed

- SQLite driver: accept pool/runtime constructor kwargs (uniform with other
  backends) and fix `_row_to_dict` row iteration (`Row.keys()` not value
  iteration).
- MySQL driver: accept pool/runtime constructor kwargs for `connect()`.

---

## [0.1.3] - 2026-06-26

### Added

- **SQL Server (thin parity)**: `ferrum-orm[mssql]` extra with `aioodbc` driver,
  T-SQL dialect (`?` placeholders, bracket quoting, `OUTPUT INSERTED.*`,
  `OFFSET/FETCH` pagination), and migration orchestration/introspection aligned
  with MySQL/SQLite backends.
- **MessagePack wire format**: opt-in Python↔Rust IR/hydration serialization via
  `ferrum-orm[msgpack]` and `FERRUM_WIRE_FORMAT=msgpack` or `[ferrum] wire_format`
  in `ferrum.toml` / `pyproject.toml` (JSON remains the default).
- **Ticket-analyzer compatibility**: composite primary keys, array/JSONB field types,
  `QuerySet.upsert()` / `bulk_upsert()`, RLS/tenant session helpers, `call_function`,
  migration ops for extensions/RLS/function DDL, and `ferrum.ext.pgvector.vector_search()`
  with per-row similarity scores.
- **Production runtime (Phase 4)**: `connect()` / `Connection` accept `acquire_timeout`,
  `query_timeout`, `statement_timeout` (ms), `max_lifetime`, `drain_timeout`, and an
  explicit opt-in `RetryPolicy` (default: no retries). Queries time out at the Python
  await point with `FerrumTimeoutError`; graceful shutdown drains in-flight work before
  closing the pool.
- **`Connection.health_check()`**: cheap `SELECT 1` liveness probe.
- **`ferrum.observability`**: `enable_metrics()`, `get_metrics()`, and optional
  `enable_opentelemetry()` bridging Tier-A hook fields to in-process metrics / OTel
  (`ferrum-orm[otel]` extra).
- **`RetryPolicy`**: configurable retry categories (`deadlock`, `connection`,
  `serialization`) — explicit opt-in only.
- **Bulk APIs**: `QuerySet.bulk_create()`, `bulk_update()`, and `bulk_delete()` with
  `batch_size` chunking, Rust-compiled multi-row SQL, and optional `returning=False`
  count mode for `bulk_create`.
- **Migrations maturity**: migration checksum verification on apply/revert;
  `ferrum sqlmigrate` offline SQL rendering; `AlterColumn` operation; `showmigrations`
  checksum-mismatch indicator; schema drift warning at `ferrum migrate` when models
  are loaded; `revert --target` walks applied migrations down to a named target.
- IR v2 predicate trees: `Q` objects with `&` / `|` / `~`, `exclude()`, `distinct()`,
  `exists()`, `values()` / `values_list()`, `only()` / `defer()` (deferred field access
  raises `FerrumDeferredFieldError`), and QuerySet slicing.
- Relationship loading: `select_related()` (FK/O2O JOIN), `prefetch_related()` (reverse FK /
  M2M batched queries), forward relation instance access, and `FerrumRelationNotLoadedError` when
  a relation was not loaded.

### Changed

- QuerySet IR version bumped to **2** (predicate / distinct / exists nodes).

---

## [0.1.2] - 2026-06-19

### Added

- `Connection.transaction()` and `Transaction`: async context manager for units of work
  with commit-on-success / rollback-on-error, optional isolation / readonly / deferrable
  modifiers, optional deadline, and nested `savepoint()` support (PostgreSQL/asyncpg).
- QuerySet terminals accept a `Transaction` anywhere a `Connection` is accepted so
  multiple statements share one pinned connection inside a transaction.

### Fixed

- Upgraded PyO3 from 0.22 to 0.29, resolving RUSTSEC-2025-0020 and RUSTSEC-2026-0177;
  removed the corresponding `deny.toml` advisory ignores.

### Changed

- PyO3 0.29 API migration in `ferrum-pyo3` (`Bound` → unbound types, `IntoPyObjectExt`).

---

## [0.1.1] - 2026-06-18

### Fixed

- Row hydration: build the model dict from `row.keys()` instead of iterating the
  row. An asyncpg `Record` iterates _values_, not column names, so reads and
  `create()` raised `TypeError: keywords must be strings` / `KeyError`.
- Migration replay guard now catches the driver-mapped `FerrumIntegrityError`
  (duplicate digest) and surfaces `FerrumMigrationError`.

### Changed

- Multi-database drivers (`pg`/`mysql`/`sqlite` extras) with a uniform driver
  protocol; the connection pool now lives behind the driver.
- CI/packaging: ty/ruff fixes for the driver code, native ARM64 wheel build,
  and `pg` extra installed where the suite imports `asyncpg`.

---

[Unreleased]: https://github.com/ferrum-orm/ferrum/compare/v0.1.6...HEAD
[0.1.6]: https://github.com/ferrum-orm/ferrum/compare/v0.1.5...v0.1.6
[0.1.5]: https://github.com/ferrum-orm/ferrum/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/ferrum-orm/ferrum/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/ferrum-orm/ferrum/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/ferrum-orm/ferrum/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/ferrum-orm/ferrum/compare/v0.1.0...v0.1.1## [Unreleased]

### Added

### Changed

### Fixed

---

## [0.1.18] - 2026-08-29
# Changelog
All notable changes to Ferrum are documented in this file.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Ferrum versions as 0.x under [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
with SemVer's unstable-major rule: **0.x has no compatibility guarantee**. MINOR and
PATCH may both break. After the first 1.0 release, removing or breaking a documented
public Python API or the migration file/ops format requires a deprecation window of at
least one minor release and 90 days, whichever is longer, recorded here and in
`README.md`. Generated SQL is never a compatibility surface.


## [Unreleased]

### Added

- Live multi-driver integration coverage for PostgreSQL, MySQL, SQLite, and SQL
  Server, with PostgreSQL + SQLite required on pull requests and an independent
  four-backend nightly matrix.

### Changed

- Local database tooling now starts health-checked PostgreSQL, MySQL, and SQL
  Server test services with the same `ferrum_test` credentials used by CI.
- Backend support is documented and tested as an explicit capability matrix
  matching the integration registry: PostgreSQL has full coverage; MySQL and
  SQL Server claim FTS and composite primary keys only; SQLite additionally
  claims `RETURNING`. Transactions, aggregates, and upsert are PostgreSQL-only.
- Compatibility policy: 0.x carries no guarantee (MINOR and PATCH may both break);
  1.0 starts a deprecation window of one minor + 90 days for public API and
  migration-ops. Generated SQL remains a non-compatibility surface.

### Fixed

- Upsert attempts on MySQL, SQLite, and SQL Server now fail early with a typed
  Ferrum configuration error instead of sending PostgreSQL `ON CONFLICT` SQL to
  an unsupported backend.
- SQLite now drains `RETURNING` cursors before commit, cleans up cancelled
  operations, and hydrates integer-backed boolean columns as `bool`. Relation
  prefetch SQL now uses each driver's identifier quoting and parameter style
  instead of PostgreSQL-only syntax.

---

## [0.1.17] - 2026-08-10

### Added

### Changed

### Fixed

- Vector writes now bind a pgvector text literal instead of a raw `list[float]`.
  asyncpg has no codec for the `vector` type and falls back to text, so
  `create()`, `update()`, `bulk_create()`, `bulk_update()`, and `upsert()`
  raised `FerrumDatabaseError: DataError` ("expected str, got list") on any
  `Vector` field. Read-path `nearest_to()` / `vector_search()` already encoded
  the literal; the write paths did not.
- `ferrum.ext.pgvector.register_vector_codecs()` now registers the `vector`
  codec pool-wide. asyncpg exposes `set_type_codec` on a connection rather than
  a pool, so the previous fallback configured only the one connection it
  acquired — every other pooled connection decoded `vector` columns as `str`,
  making a read's result depend on which connection served it. The asyncpg
  driver gained `add_type_codec()`, which applies codecs from the pool `init`
  hook and expires live connections so the registration is uniform. The
  `timeout` argument now actually bounds the `CREATE EXTENSION` statement
  (it was previously assembled into an unused string).

---

## [0.1.16] - 2026-08-09

### Added

### Changed

### Fixed

- `bulk_update()` now casts each `VALUES` placeholder to its DDL column type
  instead of a lossy approximation. A `uuid` primary key compiled to
  `$1::text`, so the `t.pk = v.pk` join raised
  `FerrumDatabaseError: UndefinedFunctionError` (PostgreSQL 42883); `uuid[]`
  and `tsvector` columns failed the `SET` assignment, and `numeric` columns
  failed parameter binding against a `double precision` cast.

---

## [0.1.15] - 2026-07-26

### Added

### Changed

### Fixed

---

## [0.1.14] - 2026-07-26

### Added

### Changed

### Fixed

---

## [0.1.13] - 2026-07-26

### Added

- Typed `QuerySet` aggregates (`count`/`sum`/`avg`/`min`/`max`) with grouping,
  filtered aggregates, date buckets, bound `HAVING`, and structured dict rows.
- Filtered `update_returning()` for atomic compare-and-set claims.
- Async context-managed `QuerySet.stream()` with bounded PostgreSQL chunks,
  normal projection/deferred materialization, and deterministic cursor cleanup.
- `Field(jsonb_list=True)`, generated/read-only field metadata, and read-only
  PostgreSQL schema-fidelity drift reporting.

### Changed

- Create/update/upsert/bulk writes now derive their assignment allowlist from
  `ModelMetadata.writable_fields`; generated/read-only columns remain selectable
  and hydratable but cannot be written.

### Fixed

---

## [0.1.12] - 2026-07-26

### Fixed

- **Inherited relation descriptors**: `ClassVar` `ForeignKey` / `OneToOne` /
  `ManyToMany` declared on a parent model are now collected via MRO, so
  subclasses (e.g. `Ticket(TicketRead)` with `team` on `TicketRead`) support
  `filter(team__slug=...)` and `select_related("team")`.

---

## [0.1.11] - 2026-07-26

### Added

- **Relation-filter JOINs**: Django-style one-level lookups
  (`filter(team__slug=...)`, `Q(team__slug=...) | Q(team__id=...)`) auto-INNER-JOIN
  the related FK/OTO table. Combines with `select_related()` (reuses LEFT JOIN).
  Nested hops and relation lookups on UPDATE/DELETE are rejected.

### Changed

### Fixed

---

## [0.1.10] - 2026-07-26

### Added

- **`QuerySet.project(model)`**: hydrate rows into a different model that maps to
  the same table (e.g. `Ticket.objects.nearest_to(...).project(TicketRead)`).
  SELECT is restricted to shared fields; filters / KNN compile against the source.
- **SQL echo / verbose mode** (SQLAlchemy-like): `ferrum.enable_echo()`,
  `ferrum.connect(..., echo=True|"debug")`, or `FERRUM_ECHO=1|debug`. Prints
  compiled SQL (+ param types; bound values only in debug/verbose). Generic
  `DEBUG=1` never enables echo.

### Changed

- **`nearest_to()` + `order_by()`**: KNN distance is the primary `ORDER BY` key;
  plain `order_by()` columns are secondary tie-breakers (previously `order_by`
  silently dropped `vector_order_by`).
- **`nearest_to()` bind format**: query vectors are bound as pgvector text
  literals with an SQL `$N::vector` cast, fixing asyncpg `float[]` → `DataError`
  against pgvector columns.

### Fixed

---

## [0.1.9] - 2026-07-12

### Added

### Changed

### Fixed

---

## [0.1.8] - 2026-07-10

### Added

### Changed

### Fixed

---

## [0.2.0] - 2026-07-10

### Added

- **Instance input for `create()`**: `Model.objects.create(conn, instance_or_dict)`
  inserts a model instance (or dict) directly, mirroring `bulk_create()` semantics —
  values from `model_dump()`, auto-PK sentinel (`0`/`None`/`""`) dropped so the DB
  default runs. The kwargs form is unchanged. Mixing both forms raises
  `FerrumCompileError`.
- **`QuerySet.update_instance(conn, obj, *, fields=None)`**: persist one instance's
  field values to its row by primary key (composite PKs supported). Returns the
  affected row count; `0` signals a missing/stale row.

### Fixed

- `update()` / `delete()` now raise `FerrumCompileError` on a sliced queryset
  (`qs[:10]`) or one carrying `select_related()`/`nearest_to()`/`rank_by()` state.
  Previously `LIMIT`/`OFFSET` and join state were silently dropped from the write
  IR — `filter(...)[:10].delete()` deleted **all** matching rows.
- An insert whose row is empty after auto-PK sentinel dropping now fails with a
  structured `FerrumCompileError` before SQL emission instead of surfacing a raw
  PostgreSQL syntax error (`INSERT INTO t () VALUES ()`), for both `create()` and
  `bulk_create()`.
- Instance write paths (`create(conn, obj)`, `bulk_create`, `bulk_update`,
  `update_instance`) reject instances carrying deferred (`only()`/`defer()`)
  fields — `model_dump()` bypasses the deferred-field guard and would silently
  write class defaults for columns that were never loaded.

---

## [0.1.6] - 2026-07-01

### Fixed

- **inspectdb**: emit FK backing columns (`{name}_id`) and `ClassVar[ForeignKey]`
  relationship descriptors instead of invalid instance-field `ForeignKey`
  annotations that broke Pydantic model construction.

---

## [0.1.5] - 2026-07-01

### Added

- **Cross-dialect full-text search (ADR-007, IR v3)**: `match`, `match_phrase`,
  `match_websearch`, and `match_boolean` filter operators; `QuerySet.rank_by()` /
  `search()` for relevance ordering; `FullTextIndex` model declaration;
  `CreateFullTextIndex` / `DropFullTextIndex` / `CreateFullTextCatalog` migration
  ops; per-dialect emit in `ferrum-sql/src/fts/` and DDL in
  `ferrum.migrations.fts`; optional `ferrum.ext.fts.scored_search` helper.
- **`Field(fts_config=...)`** for PostgreSQL `regconfig` allowlisting on
  `TSVector` / indexed text fields.

### Changed

- **IR version 2 → 3** — adds optional `text_rank_by` node. Python `_IR_VERSION`
  and Rust `IR_VERSION` must stay synchronized.

### Fixed

- **inspectdb**: query `information_schema` via `driver.fetch()` instead of
  reaching into a private `_pool` on the timed query executor.

### Breaking

- **QuerySet IR version bump (2 → 3).** Any code that constructs or validates raw IR
  JSON (custom tooling, pinned compiler tests) must accept the new optional
  `text_rank_by` field. The public Python API is backward-compatible — existing
  filter-only FTS lookups continue to work; ranking is opt-in via `rank_by()` /
  `search()`.

---

## [0.1.4] - 2026-07-01

### Added

- **Examples**: `examples/sqlite/` (file-based SQLite, no Docker),
  `examples/mysql/` (`mysql://` + Docker Compose), and
  `examples/pyproject_config/` (`[ferrum]` in `pyproject.toml` with
  `database_url_env = "DATABASE_URL"`). Updated `examples/README.md` with a
  driver extras matrix.

### Changed

- `ferrum-migrate` crate version now follows `[workspace.package]` like the
  other workspace members.

### Fixed

- SQLite driver: accept pool/runtime constructor kwargs (uniform with other
  backends) and fix `_row_to_dict` row iteration (`Row.keys()` not value
  iteration).
- MySQL driver: accept pool/runtime constructor kwargs for `connect()`.

---

## [0.1.3] - 2026-06-26

### Added

- **SQL Server (thin parity)**: `ferrum-orm[mssql]` extra with `aioodbc` driver,
  T-SQL dialect (`?` placeholders, bracket quoting, `OUTPUT INSERTED.*`,
  `OFFSET/FETCH` pagination), and migration orchestration/introspection aligned
  with MySQL/SQLite backends.
- **MessagePack wire format**: opt-in Python↔Rust IR/hydration serialization via
  `ferrum-orm[msgpack]` and `FERRUM_WIRE_FORMAT=msgpack` or `[ferrum] wire_format`
  in `ferrum.toml` / `pyproject.toml` (JSON remains the default).
- **Ticket-analyzer compatibility**: composite primary keys, array/JSONB field types,
  `QuerySet.upsert()` / `bulk_upsert()`, RLS/tenant session helpers, `call_function`,
  migration ops for extensions/RLS/function DDL, and `ferrum.ext.pgvector.vector_search()`
  with per-row similarity scores.
- **Production runtime (Phase 4)**: `connect()` / `Connection` accept `acquire_timeout`,
  `query_timeout`, `statement_timeout` (ms), `max_lifetime`, `drain_timeout`, and an
  explicit opt-in `RetryPolicy` (default: no retries). Queries time out at the Python
  await point with `FerrumTimeoutError`; graceful shutdown drains in-flight work before
  closing the pool.
- **`Connection.health_check()`**: cheap `SELECT 1` liveness probe.
- **`ferrum.observability`**: `enable_metrics()`, `get_metrics()`, and optional
  `enable_opentelemetry()` bridging Tier-A hook fields to in-process metrics / OTel
  (`ferrum-orm[otel]` extra).
- **`RetryPolicy`**: configurable retry categories (`deadlock`, `connection`,
  `serialization`) — explicit opt-in only.
- **Bulk APIs**: `QuerySet.bulk_create()`, `bulk_update()`, and `bulk_delete()` with
  `batch_size` chunking, Rust-compiled multi-row SQL, and optional `returning=False`
  count mode for `bulk_create`.
- **Migrations maturity**: migration checksum verification on apply/revert;
  `ferrum sqlmigrate` offline SQL rendering; `AlterColumn` operation; `showmigrations`
  checksum-mismatch indicator; schema drift warning at `ferrum migrate` when models
  are loaded; `revert --target` walks applied migrations down to a named target.
- IR v2 predicate trees: `Q` objects with `&` / `|` / `~`, `exclude()`, `distinct()`,
  `exists()`, `values()` / `values_list()`, `only()` / `defer()` (deferred field access
  raises `FerrumDeferredFieldError`), and QuerySet slicing.
- Relationship loading: `select_related()` (FK/O2O JOIN), `prefetch_related()` (reverse FK /
  M2M batched queries), forward relation instance access, and `FerrumRelationNotLoadedError` when
  a relation was not loaded.

### Changed

- QuerySet IR version bumped to **2** (predicate / distinct / exists nodes).

---

## [0.1.2] - 2026-06-19

### Added

- `Connection.transaction()` and `Transaction`: async context manager for units of work
  with commit-on-success / rollback-on-error, optional isolation / readonly / deferrable
  modifiers, optional deadline, and nested `savepoint()` support (PostgreSQL/asyncpg).
- QuerySet terminals accept a `Transaction` anywhere a `Connection` is accepted so
  multiple statements share one pinned connection inside a transaction.

### Fixed

- Upgraded PyO3 from 0.22 to 0.29, resolving RUSTSEC-2025-0020 and RUSTSEC-2026-0177;
  removed the corresponding `deny.toml` advisory ignores.

### Changed

- PyO3 0.29 API migration in `ferrum-pyo3` (`Bound` → unbound types, `IntoPyObjectExt`).

---

## [0.1.1] - 2026-06-18

### Fixed

- Row hydration: build the model dict from `row.keys()` instead of iterating the
  row. An asyncpg `Record` iterates _values_, not column names, so reads and
  `create()` raised `TypeError: keywords must be strings` / `KeyError`.
- Migration replay guard now catches the driver-mapped `FerrumIntegrityError`
  (duplicate digest) and surfaces `FerrumMigrationError`.

### Changed

- Multi-database drivers (`pg`/`mysql`/`sqlite` extras) with a uniform driver
  protocol; the connection pool now lives behind the driver.
- CI/packaging: ty/ruff fixes for the driver code, native ARM64 wheel build,
  and `pg` extra installed where the suite imports `asyncpg`.

---

[Unreleased]: https://github.com/ferrum-orm/ferrum/compare/v0.1.6...HEAD
[0.1.6]: https://github.com/ferrum-orm/ferrum/compare/v0.1.5...v0.1.6
[0.1.5]: https://github.com/ferrum-orm/ferrum/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/ferrum-orm/ferrum/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/ferrum-orm/ferrum/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/ferrum-orm/ferrum/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/ferrum-orm/ferrum/compare/v0.1.0...v0.1.1