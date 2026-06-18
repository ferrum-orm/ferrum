# Changelog

All notable changes to Ferrum are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Ferrum uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Added
- Repository scaffold: monorepo layout, Cargo workspace, pyproject.toml, maturin configuration.
- `ferrum-core` crate: IR types (`QuerySetIR`, `ModelMetadata`), allowlist-based compiler skeleton, hydration and migration plan placeholders.
- `ferrum-sql` crate: PostgreSQL dialect (identifier quoting, placeholder style), `emit_select` with full WHERE/ORDER BY/LIMIT/OFFSET emission and SQL-2 verified tests.
- `ferrum-pyo3` crate: thin PyO3 bridge with `compile_query`, structured error types (`FerrumInternalError`, `FerrumCompileError`), panic catch wrapper.
- `python/ferrum` package skeleton: `Model`, `QuerySet` (danger-API guards), `Connection` (redacted DSN diagnostics), `hooks` (Tier A/B/C dispatcher with redaction), `errors` (complete taxonomy), `migrations` (orchestrator, ledger, tokens, gates), `cli` (init scaffold, migrations subcommand), `contrib/fastapi` lifespan helper.
- Test suite scaffold: unit, integration, property, and security qualification layers.
- Security qualification tests covering SQL-1/2/3, CRED-1, LOG-1/2, MIG-1/2/5/6/7/8, INIT-1/2.
- CI workflows: `ci.yml` (PR gate), `release.yml` (abi3 wheel + publish), `nightly.yml` (full matrix + audit + benchmarks).
- Developer tooling: `rustfmt.toml`, `deny.toml`, `.importlinter`, `.pre-commit-config.yaml`, `Makefile`.

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

[Unreleased]: https://github.com/ferrumdb/ferrum/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/ferrumdb/ferrum/compare/v0.1.0...v0.1.1
