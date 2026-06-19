# Changelog

All notable changes to Ferrum are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Ferrum uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

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
