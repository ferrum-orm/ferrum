# Contributing to Ferrum

Thank you for taking the time to improve Ferrum. This project is an alpha-stage async ORM with
a Python API, a Rust native extension, and a PostgreSQL-first runtime, so the contribution bar is
mostly about preserving boundaries: async I/O stays in Python, compilation and codecs stay pure in
Rust, and security-sensitive surfaces stay redacted and test-covered.

This guide should get a first contribution from clone to a focused pull request in under 30
minutes on macOS or Linux. Windows is supported by CI, but local extension builds can need extra
linker setup; see the Windows notes below.

## Start Here

Before making a non-trivial change, read these files:

1. `README.md` for the public API shape and roadmap.
2. `docs/architecture.md` for the Python/Rust boundary and query lifecycle.
3. `AGENTS.md` for the binding engineering and security contract.
4. The relevant example under `examples/` if you are changing user-facing behavior.

`AGENTS.md` is intentionally strict because it captures the invariants reviewers will enforce.
If this guide and `AGENTS.md` disagree, treat `AGENTS.md` as the source of truth and call out the
drift in your PR.

## Project Shape

| Path                     | Purpose                                                                                                           |
| ------------------------ | ----------------------------------------------------------------------------------------------------------------- |
| `python/ferrum/`         | Public Python package: models, `QuerySet`, connection pool, errors, hooks, migrations, CLI, contrib integrations. |
| `crates/ferrum-core/`    | Pure Rust IR validation, compilation, hydration, and migration planning types.                                    |
| `crates/ferrum-sql/`     | PostgreSQL SQL emitter and SQL benchmarks.                                                                        |
| `crates/ferrum-pyo3/`    | PyO3 bridge that exposes `ferrum._native`; this is the only crate that depends on PyO3.                           |
| `crates/ferrum-migrate/` | Rust-side migration planning support.                                                                             |
| `tests/python/`          | Python unit, property, integration, security, benchmark, and smoke tests.                                         |
| `docs/`                  | Public documentation.                                                                                             |
| `examples/`              | Runnable examples for CRUD, migrations, FastAPI, and compatibility scenarios.                                     |
| `mise.toml`              | Canonical local task runner, mirroring CI.                                                                        |

## Quick Start

Prerequisites:

- Python 3.11 or newer. CI currently tests Python 3.11, 3.12, and 3.13.
- Rust stable. The repository pins Rust 1.87.0 in `rust-toolchain.toml`.
- `uv` for Python dependency management.
- `mise` for project tasks. It is optional, but it is the preferred interface.
- PostgreSQL 14 or newer for integration and security tests. Unit tests do not need a database.
- Docker if you want to use the example PostgreSQL compose files.

Set up the development environment:

```bash
git clone https://github.com/ferrumdb/ferrum.git
cd ferrum

uv sync --extra dev
mise run dev
```

`mise run dev` runs `uv run maturin develop` and installs the native extension as
`ferrum._native` inside the project virtualenv.

Verify the extension:

```bash
uv run python -c "import ferrum._native; print('ok')"
```

Run the fast local tests:

```bash
mise run test-python-unit
mise run test-rust
```

Before opening a substantial PR, run the scoped verification commands listed below. Before asking
for merge on a broad change, run:

```bash
mise run ci-local
```

## Local PostgreSQL

Integration and security tests need a live PostgreSQL database. The examples include a local
compose file:

```bash
cd examples/simple
cp .env.example .env
docker compose up -d
cd ../..

export FERRUM_DATABASE_URL=postgresql://ferrum:changeme@127.0.0.1:5432/ferrum_dev
export FERRUM_TEST_DSN="$FERRUM_DATABASE_URL"
```

`FERRUM_DATABASE_URL` is the application and CLI DSN. `FERRUM_TEST_DSN` is used by the integration
test suite. Ferrum must never log full DSNs, passwords, bound values, or row data by default.

Useful database-backed checks:

```bash
mise run test-integration
mise run test-security
```

## Development Workflow

Keep PRs focused. One behavior change, bug fix, or documentation improvement is easier to review
than a wide refactor with unrelated cleanup.

Recommended flow:

1. Fork or branch from `main`.
2. Make the smallest change that satisfies the issue or proposal.
3. Add or update tests with the change.
4. Update `README.md`, `docs/`, examples, or docstrings when public behavior changes.
5. Run the smallest verification that proves the change.
6. Open a PR that states the problem, solution, architecture impact, security impact, and test
   command.

Branch names can be simple: `fix-queryset-count`, `docs-fastapi-example`, or
`issue-123-migration-gate`.

Commit messages should be imperative and explain the intent. Do not commit secrets, `.env` files,
private keys, generated databases, build artifacts, or local IDE metadata.

External contributors should use their normal GitHub identity. Maintainer-only release and merge
automation may use the `ferrum-orm` bot identity; that is not a requirement for community PRs.

## Scoped Verification

Use the smallest command set that covers the risk of your change.

| Change area                                                    | Minimum verification                                                                              |
| -------------------------------------------------------------- | ------------------------------------------------------------------------------------------------- |
| Documentation only                                             | Manual review of rendered Markdown and links.                                                     |
| Rust-only change in `ferrum-core` or `ferrum-sql`              | `mise run test-rust lint-rust boundary`                                                           |
| Python API change without PostgreSQL behavior                  | `mise run dev test-python-unit type-python lint-python import-boundary`                           |
| QuerySet IR, PyO3 boundary, or cross-language behavior         | `mise run dev test-python-unit test-rust test-integration boundary`                               |
| Connection pool, transactions, drivers, or PostgreSQL behavior | `mise run dev test-integration test-python-unit`                                                  |
| SQL compilation, hooks, errors, credentials, or migrations     | `mise run dev test-python-unit test-integration test-security`                                    |
| Public API surface                                             | Relevant checks above, plus `README.md` and `docs/` updates.                                      |
| Performance work                                               | Relevant checks above, plus `cargo bench` or `uv run pytest tests/python/benchmark -m benchmark`. |

Full local CI parity:

```bash
mise run ci-local
```

That task runs Rust lint/check/tests, Python lint/type/import-boundary, boundary checks, extension
build, Python unit/property tests, integration tests, and security tests.

## Testing Expectations

No ORM feature ships without tests.

Test directories:

- `tests/python/unit/` for fast Python behavior and CLI/model/query tests.
- `tests/python/property/` for Hypothesis tests.
- `tests/python/integration/` for live PostgreSQL behavior.
- `tests/python/security/` for release-qualification gates.
- `tests/python/benchmark/` for non-blocking performance work.
- Rust unit tests live with the crates; CI runs `cargo test -p ferrum-core -p ferrum-sql`.

Pytest markers:

- `integration` requires PostgreSQL.
- `security` is a release gate.
- `property` covers property-based tests.
- `benchmark` is for performance measurements and is not a default blocking gate.
- `smoke` covers installation/import checks.

Local pytest defaults exclude integration and security tests unless you request them explicitly.
Use the `mise` tasks so local verification matches CI.

## Architecture Rules

Ferrum's architecture is intentionally narrow.

- Python owns public API ergonomics, Pydantic models, `QuerySet`, async I/O, connection pooling,
  transactions, hook dispatch, error mapping, migration orchestration, and CLI behavior.
- Rust owns pure, synchronous, stateless compilation and codec work.
- `ferrum-core` and `ferrum-sql` must not depend on `pyo3` or `tokio`; run `mise run boundary`.
- Rust does not perform I/O and does not hold per-request mutable shared state.
- The PyO3 bridge maps Rust errors and panics to catchable Python exceptions.
- Core APIs are async-first. Do not add sync wrappers or blocking compatibility layers.
- Pydantic v2 models are the schema source of truth. Do not add duplicate persistence schemas.
- PostgreSQL is the primary development and CI target.

The Python import boundaries are enforced by `.importlinter`. In particular, core query modules
must not import CLI, contrib, FastAPI, Starlette, Typer, or Rich.

## Security Rules

Security-sensitive changes need extra care and tests. This includes SQL compilation, migrations,
hooks, errors, credentials, connection diagnostics, and PyO3 error boundaries.

Hard rules:

- Never interpolate user input into SQL identifiers or values.
- Identifiers must come from model metadata allowlists.
- Values must be emitted as bound parameters.
- Unknown fields, unsupported operators, and invalid sort directions must fail before SQL emission.
- Default observability payloads are Tier A only: identifiers, timing, status, and failure category.
- Bound values, full DSNs, passwords, row data, and raw PostgreSQL details must not appear in
  default hooks, errors, logs, or migration output.
- Tier B and Tier C observability require Ferrum-specific opt-in. Never enable them from a generic
  `DEBUG=1`. Tier C is local-dev only.
- Migration apply paths must preserve dry-run, destructive-operation confirmation, and non-dev
  environment confirmation gates.
- Unscoped `update()` and `delete()` must fail by default; all-row writes go through the named
  danger APIs.

If you touch one of these areas, include a `Security Impact` note in your PR and run:

```bash
mise run test-security
```

## Documentation Requirements

Public API changes require documentation updates in the same PR.

Update the relevant docs when you change:

- Top-level `ferrum` exports or import paths.
- Model, field, relation, connection, transaction, hook, migration, or CLI behavior.
- QuerySet chaining methods, terminal methods, operators, or error behavior.
- Installation extras, environment variables, or example setup.

Common targets:

- `README.md` for the public pitch, install snippets, quick examples, and roadmap.
- `docs/getting-started.md` for walkthrough behavior.
- `docs/api-reference.md` for public API details.
- `docs/architecture.md` for boundary or lifecycle changes.
- `examples/*/README.md` and example code for runnable workflows.
- `CHANGELOG.md` for user-facing changes.

`docs/docstring-coverage.md` tracks public-symbol docstring coverage. Keep public classes and
functions documented.

## Examples

Examples are part of the contributor and user experience. If you change user-facing behavior,
consider whether an example should change too.

- `examples/simple/` demonstrates async CRUD without a web framework.
- `examples/migrations/` demonstrates `makemigrations`, `migrate`, `showmigrations`, destructive
  gates, and the legacy JSON plan path.
- `examples/fastapi_quickstart/` demonstrates ASGI lifespan integration.
- `examples/ticket_analyzer_compat/` exercises composite primary keys, upsert, array/JSONB fields,
  RLS helpers, pgvector, stored procedure calls, and richer migration operations.

Run examples after building the extension:

```bash
uv sync --extra dev
mise run dev
```

Then follow the README in the example directory.

## Pull Requests

A good PR description includes:

- Problem: what bug, gap, or user need this addresses.
- Solution: what changed and why.
- Architecture impact: whether the Python/Rust boundary, IR, or public API changed.
- Performance impact: whether a hot path, allocation pattern, query count, or benchmark changed.
- Security impact: whether SQL, hooks, errors, credentials, or migrations changed.
- Migration impact: whether schema or migration behavior changed.
- Tests: exact command(s) run locally.
- Docs: files updated or why docs are not needed.

Draft PRs are welcome for early feedback. Ready-for-review PRs should have scoped verification
passing and should avoid unrelated formatting or cleanup.

Breaking changes, new public APIs, query-language changes, migration-engine changes, or decisions
that alter the Python/Rust boundary may require an RFC or maintainer design discussion before
implementation.

## Issues

Good issue reports include:

- Ferrum version or commit SHA.
- Python version, OS, and database version when relevant.
- Whether `ferrum._native` was built with `maturin develop`.
- A minimal reproduction, preferably as a failing test or small script.
- Expected behavior and actual behavior.
- Error output with secrets and DSNs redacted.

For security vulnerabilities, do not post exploit details in a public issue. Open a private
security advisory or contact the maintainers through the repository's configured private channel
once `SECURITY.md` is published.

## Windows Notes

CI verifies Windows sdist builds and unit/property tests, but the local path is more fragile than
macOS/Linux because PyO3 may need a `python3.lib` linker import library.

If you are on Windows:

- Use the Rust toolchain from `rust-toolchain.toml`.
- Prefer `maturin develop` over editable PEP 517 installs for the extension.
- If the linker cannot find `python3.lib`, copy the matching `python*.lib` from your Python
  installation's `libs` directory to `python3.lib` and make sure that directory is on `LIB`.
- Run unit/property tests locally and rely on CI for PostgreSQL integration coverage if local
  database setup is not practical.

## First Contribution Ideas

Good first contributions are usually scoped and low risk:

- Improve a docs page or example README.
- Add a missing edge-case unit test in `tests/python/unit/`.
- Improve an error message without changing the error taxonomy.
- Add a Rust unit test or property test for an existing compiler behavior.
- Improve `docs/docstring-coverage.md` reproduction instructions if the public API changes.

Avoid using a first PR for PyO3 boundary changes, migration apply gates, SQL emitters, credential
handling, or observability redaction. Those areas are welcome, but they need maintainer pairing.

## Release Notes and Attribution

Ferrum is Apache-2.0 licensed. By contributing, you agree that your contribution is provided under
the project license.

User-facing changes should update `CHANGELOG.md` under `[Unreleased]` when that section exists or
follow the existing changelog style. Maintainers may squash-merge PRs, but release notes should
credit external contributors by GitHub handle when possible.

## Getting Help

Use GitHub issues for actionable bugs and scoped feature requests. Use GitHub Discussions when
available for questions, design sketches, and RFC-style conversations.

When in doubt, open a draft PR with a failing test or a small reproduction. It is easier to review
a concrete, scoped change than to infer intent from a broad proposal.
