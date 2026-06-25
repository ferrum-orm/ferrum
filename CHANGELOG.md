# Changelog

All notable changes to Ferrum are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Ferrum uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Added
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
- IR v2 predicate trees: ``Q`` objects with ``&`` / ``|`` / ``~``, ``exclude()``, ``distinct()``,
  ``exists()``, ``values()`` / ``values_list()``, ``only()`` / ``defer()`` (deferred field access
  raises ``FerrumDeferredFieldError``), and QuerySet slicing.
- Relationship loading: ``select_related()`` (FK/O2O JOIN), ``prefetch_related()`` (reverse FK /
  M2M batched queries), forward relation instance access, and ``FerrumRelationNotLoadedError`` when
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
  row. An asyncpg `Record` iterates *values*, not column names, so reads and
  `create()` raised `TypeError: keywords must be strings` / `KeyError`.
- Migration replay guard now catches the driver-mapped `FerrumIntegrityError`
  (duplicate digest) and surfaces `FerrumMigrationError`.

### Changed
- Multi-database drivers (`pg`/`mysql`/`sqlite` extras) with a uniform driver
  protocol; the connection pool now lives behind the driver.
- CI/packaging: ty/ruff fixes for the driver code, native ARM64 wheel build,
  and `pg` extra installed where the suite imports `asyncpg`.

---

[Unreleased]: https://github.com/ferrumdb/ferrum/compare/v0.1.2...HEAD
[0.1.2]: https://github.com/ferrumdb/ferrum/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/ferrumdb/ferrum/compare/v0.1.0...v0.1.1
