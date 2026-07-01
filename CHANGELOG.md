# Changelog

All notable changes to Ferrum are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Ferrum uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

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
