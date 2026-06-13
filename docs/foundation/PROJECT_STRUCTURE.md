# Ferrum Project Structure

**Status:** Approved (CEO board approval `e69b1ccb`, 2026-06-13)
**Version:** v0.1
**Inputs:** [ARCHITECTURE.md](./ARCHITECTURE.md), [PRODUCT_REQUIREMENTS.md](./PRODUCT_REQUIREMENTS.md), [ARCHITECTURE_FEASIBILITY_REVIEW.md](./ARCHITECTURE_FEASIBILITY_REVIEW.md), [SECURITY_REVIEW_PRD.md](./SECURITY_REVIEW_PRD.md)
**Issue:** GUY-71
**Date:** 2026-06-13

---

## 1. Purpose

This document defines the physical structure of the Ferrum repository before implementation begins: the monorepo layout, the Python package and Rust crate boundaries, the testing strategy, the CI/CD pipeline, and the developer tooling. It is the authoritative repo-scaffolding contract for v0.1.

It is subordinate to [ARCHITECTURE.md](./ARCHITECTURE.md). Where this document and the architecture differ, the architecture wins and this document must be corrected. This document does **not** re-decide architecture; it makes the architecture's component boundaries concrete on disk and in CI.

**Scope guard (YAGNI):** This is a structure contract, not source. No production ORM source is created by this task. Directory trees below are the target scaffold engineers create when implementation is approved.

---

## 2. Design Principles for the Layout

| Principle | How it shapes the structure |
|-----------|-----------------------------|
| **Single Responsibility / Separation of Concerns** | Each crate and package has one reason to change. Pure compiler logic (Rust) is isolated from the PyO3 bridge, which is isolated from async I/O (Python). |
| **Blast Radius** | The native boundary is a thin crate (`ferrum-pyo3`) so a bridge change cannot force a recompile/retest of the whole engine, and a pure-logic change does not touch FFI. |
| **Evolutionary Architecture** | Crate/package seams map to the architecture's contracts (IR, hydration, migration plan) so v0.2 work (relationships, custom backends) slots in without restructuring. |
| **Least Astonishment** | One published Python distribution (`ferrum`) with a familiar `import ferrum` surface; the native extension is an internal implementation detail, not a separately imported package. |
| **Observability First** | Test and CI structure treats the Tier A hook contract and redaction as first-class gates, not afterthoughts. |
| **Defense in Depth** | Security-qualification tests are a named, required layer in the testing pyramid and a required CI gate. |

---

## 3. Monorepo Layout

Ferrum is a single repository containing a Cargo workspace (Rust) and a maturin-built Python distribution. One repo keeps the IR contract, the compiler that implements it, and the Python API that produces it versioned and tested together (Data Gravity: the contract is the gravitational center; co-locating avoids cross-repo drift).

```text
ferrum/                              # repository root
├── Cargo.toml                       # Rust workspace manifest (members: crates/*)
├── Cargo.lock                       # committed (workspace is an application+lib mix)
├── pyproject.toml                   # maturin backend; `ferrum` distribution metadata
├── rust-toolchain.toml              # pinned Rust toolchain (reproducible CI)
├── README.md
├── LICENSE                          # Apache-2.0
├── CHANGELOG.md
├── AGENTS.md / CLAUDE.md            # agent contracts
│
├── crates/                          # Rust workspace members
│   ├── ferrum-core/                 # pure engine: IR, compile, hydrate, migrate-plan
│   ├── ferrum-sql/                  # PostgreSQL dialect SQL emission (see §5.2)
│   └── ferrum-pyo3/                 # thin PyO3 extension bridge
│
├── python/
│   └── ferrum/                      # the importable Python package (single distribution)
│       ├── __init__.py              # public re-exports (Model, QuerySet, connect, ...)
│       ├── _native.pyi              # type stub for the compiled extension
│       ├── models.py
│       ├── queryset.py
│       ├── connection.py
│       ├── hooks.py
│       ├── errors.py
│       ├── migrations/
│       ├── cli/                     # `ferrum` console-script entrypoints (init, migrations)
│       ├── contrib/                 # optional integrations (FastAPI/Starlette helpers)
│       └── py.typed                 # PEP 561 marker (ships type information)
│
├── tests/
│   ├── python/                      # pytest: unit, integration, security qualification
│   └── rust/                        # cross-crate integration tests (unit tests live in-crate)
│
├── benches/                         # criterion (Rust) + pytest-benchmark harness configs
│
├── docs/
│   ├── foundation/                  # PRD, architecture, this document, decisions
│   ├── design/                      # product design review
│   └── guides/                      # quickstart, hook integration, migration safety
│
├── examples/                        # runnable FastAPI/Starlette example apps
│
└── .github/
    └── workflows/                   # CI/CD pipelines (see §7)
```

### 3.1 Why one Python distribution, not three

The task brief names `ferrum`, `ferrum-cli`, and `ferrum-contrib`. For v0.1 these are **modules inside the single `ferrum` distribution** (`ferrum.cli`, `ferrum.contrib`), not three separately published wheels:

- **Least Astonishment / packaging cost:** the native extension must ship in the same wheel as the API that calls it; splitting CLI and contrib into separate distributions multiplies the abi3 wheel matrix (ADR-005) and version-skew surface for no v0.1 benefit (YAGNI).
- **Evolutionary seam preserved:** `cli/` and `contrib/` are already isolated subpackages with their own dependencies declared as optional extras (`ferrum[cli]`, `ferrum[fastapi]`). If a future release needs independent cadence, they can be promoted to separate distributions without moving code — the import path and ownership boundary already exist.
- **CLI dependency isolation:** `ferrum.cli` and `ferrum.contrib` must not be imported by the core query path; enforced by an import-linter contract in CI (§6.4) so the core stays dependency-light.

This is a structural decision, not an architecture change; it refines ARCHITECTURE.md §7 (which placed `cli/` inside the package) and is recorded for `DECISIONS.md` (GUY-74).

---

## 4. Python Package Structure

Single distribution `ferrum` (published to PyPI). The compiled extension is bundled as `ferrum._native`; application code never imports it directly.

```text
python/ferrum/
├── __init__.py            # public API re-exports; version
├── _native.pyi            # typed stub for the Rust extension (hand-maintained, CI-checked)
├── models.py              # Model base, Pydantic-v2 metadata builder, immutable ModelMetadata
├── queryset.py            # lazy/chainable QuerySet; terminal coroutines; danger-API guards
├── connection.py          # asyncpg pool wrapper, DSN parse, sslmode, redacted diagnostics
├── hooks.py               # Tier A/B/C dispatcher; payload allowlist; redaction
├── errors.py              # Ferrum exception taxonomy + centralized boundary mapper
├── migrations/
│   ├── __init__.py
│   ├── orchestrator.py    # dry-run, classification, apply sequencing
│   ├── ledger.py          # migration history table access (append-only)
│   ├── tokens.py          # ADR-007 confirmation-token emit/validate (no secrets)
│   └── gates.py           # destructive + non-dev confirmation gates
├── cli/
│   ├── __init__.py        # `ferrum` console-script entrypoint (argparse/typer)
│   ├── init.py            # ADR-008 scaffold (local files only; 127.0.0.1; .gitignore .env)
│   └── migrations_cmd.py  # dry-run / apply wrappers over migrations.orchestrator
├── contrib/
│   ├── __init__.py
│   └── fastapi.py         # optional lifespan/pool helpers (extra: ferrum[fastapi])
└── py.typed
```

### 4.1 Package & Module Ownership

| Module | Owner (role) | Responsibility | Must NOT |
|--------|--------------|----------------|----------|
| `models`, `queryset` | Backend Engineer | Public API surface, IR construction | Build SQL strings; touch I/O |
| `connection` | Backend Engineer + SecurityEngineer review | Pool, DSN, TLS, redacted diagnostics | Log DSN/password |
| `hooks` | Backend Engineer + SecurityEngineer review | Tier A/B/C dispatch & redaction | Emit bound values by default |
| `errors` | Backend Engineer + SecurityEngineer review | SQLSTATE/PyO3 → Ferrum taxonomy | Echo row data / DETAIL/HINT |
| `migrations/*` | Backend Engineer + SecurityEngineer review | Dry-run, gates, token, apply | Apply without dry-run/token |
| `cli/*` | Backend Engineer | Console scripts, init scaffold | Import core query path internals as a dependency |
| `contrib/*` | Backend Engineer | Framework glue | Be imported by core modules |

### 4.2 Optional Extras

| Extra | Pulls in | For |
|-------|----------|-----|
| `ferrum[cli]` | `typer`/`rich` (TBD) | richer CLI UX; base CLI works without it |
| `ferrum[fastapi]` | nothing new (uses host's FastAPI) | `ferrum.contrib.fastapi` helpers |
| `ferrum[dev]` | test/lint/type toolchain | contributor environment |

Base `ferrum` depends only on `pydantic>=2` and `asyncpg`. CLI/contrib deps are optional so the core install stays minimal (Blast Radius for the dependency tree).

---

## 5. Rust Crate Structure

A Cargo workspace under `crates/`. The split isolates pure logic (`ferrum-core`), dialect SQL emission (`ferrum-sql`), and the FFI bridge (`ferrum-pyo3`) so each has one reason to change.

```text
crates/
├── ferrum-core/                 # pure engine — no PyO3, no I/O, no network
│   ├── Cargo.toml
│   └── src/
│       ├── lib.rs
│       ├── ir/                  # QuerySetIR (versioned), ModelMetadata, enums/indices
│       ├── compile/             # IR validation against allowlists → CompiledQuery
│       ├── hydrate/             # row bytes → typed RowPayload
│       ├── migrate/             # schema diff → migration plan + canonical plan digest
│       └── error.rs             # structured CompileError/PlanError (fields, not blobs)
│
├── ferrum-sql/                  # PostgreSQL dialect: AST → parameterized SQL text
│   ├── Cargo.toml               # depends on ferrum-core (IR/AST types)
│   └── src/
│       ├── lib.rs
│       ├── dialect.rs           # placeholder style ($1..), quoting, type names
│       └── emit.rs              # SELECT/INSERT/UPDATE/DELETE/DDL emitters
│
└── ferrum-pyo3/                 # thin bridge crate — the ONLY crate with a PyO3 dep
    ├── Cargo.toml               # depends on ferrum-core + ferrum-sql; cdylib
    └── src/
        └── lib.rs               # #[pymodule] ferrum._native; Result→exception; panic catch
```

### 5.1 Crate Responsibilities & Ownership

| Crate | Published | Owner | Responsibility | Hard constraints |
|-------|-----------|-------|----------------|------------------|
| `ferrum-core` | No (internal) | Backend Engineer (Rust) | IR types, validation, hydration payloads, migration plan + digest | No `pyo3`, no `tokio`, no I/O, no `unsafe` without justification |
| `ferrum-sql` | No (internal) | Backend Engineer (Rust) | PostgreSQL-dialect SQL emission from validated AST | Parameterized output only; never interpolate user values |
| `ferrum-pyo3` | Yes (as `ferrum._native` in the wheel) | Backend Engineer + SecurityEngineer review | Type conversion, `Result`→Python exception, panic→catchable exception | No business logic; no I/O; `panic = "unwind"` + catch wrapper |

### 5.2 Why split `ferrum-sql` from `ferrum-core` (and the ADR-002 caveat)

- **Single Responsibility:** allowlist validation (security-critical, dialect-agnostic) is a different concern from SQL string emission (dialect-specific). Splitting lets the validator be fuzzed and audited independently of textual SQL.
- **Evolutionary Architecture:** the v0.1 non-goal of multi-backend (ARCHITECTURE.md §15.2) becomes a *future crate swap* (`ferrum-mysql`) rather than a rewrite — the dialect seam already exists.
- **ADR-002 is not pre-empted:** the **IR boundary shape** (the typed contract that crosses PyO3) is owned by ADR-002 and remains undecided in detail. This split is an *internal* Rust organization decision below that boundary; if ADR-002 lands a different module decomposition, `ferrum-sql` may be merged into `ferrum-core` as a module with no change to the Python API or the wheel. Recorded as an open structural note for `DECISIONS.md` (GUY-74).

### 5.3 Workspace Conventions

- `Cargo.toml` (root) declares `[workspace]` members and shared `[workspace.dependencies]` (single pinned `pyo3`, `serde`, etc.) so versions cannot drift between crates.
- `rust-toolchain.toml` pins the toolchain channel for reproducible CI and local builds.
- `#![forbid(unsafe_code)]` in `ferrum-core` and `ferrum-sql`; `ferrum-pyo3` allows `unsafe` only where PyO3 requires it, with a documented audit comment.
- The extension is built with `abi3` features (ADR-005) so one wheel covers CPython 3.11+.

---

## 6. Testing Strategy

Per AGENTS.md §2.7, no feature ships without tests, and security paths are release-qualification gates. The pyramid below names tooling and assigns each layer a CI gate.

```text
                ┌───────────────────────────────┐
                │   E2E / Example apps (few)    │  pytest + real Postgres (docker)
                ├───────────────────────────────┤
                │   Security qualification      │  pytest (required gate, see §6.3)
                ├───────────────────────────────┤
                │   Integration (Python↔Rust↔PG)│  pytest + testcontainers/asyncpg
                ├───────────────────────────────┤
                │   Property-based               │  proptest (Rust) + hypothesis (Py)
                ├───────────────────────────────┤
                │   Unit (many, fast)           │  cargo test (Rust) + pytest (Py)
                └───────────────────────────────┘
                Benchmarks (out-of-band): criterion + pytest-benchmark
```

### 6.1 Layers, Tooling, and What They Prove

| Layer | Tool | Scope | Proves |
|-------|------|-------|--------|
| **Rust unit** | `cargo test` (in-crate `#[cfg(test)]`) | `ferrum-core`, `ferrum-sql` pure functions | Compiler emits correct parameterized SQL; allowlist rejects unknown fields/operators/sorts before any SQL exists |
| **Rust property** | `proptest` | IR → SQL invariants | No input produces unparameterized values; identifiers always come from metadata indices (SQL-1/SQL-2) |
| **Python unit** | `pytest` | `models`, `queryset`, `hooks`, `errors` in isolation (native mocked where needed) | API ergonomics, IR construction, danger-API guards, error mapping |
| **Python property** | `hypothesis` | QuerySet build + error taxonomy | Arbitrary filter chains never bypass guards; error mapping is total |
| **Integration** | `pytest` + ephemeral PostgreSQL (`testcontainers` or service container) | Full read/write/migration path through the real extension and real PG | Hydration correctness, transactionality (ADR-004), migration apply/gates |
| **Security qualification** | `pytest` (tagged `security`) | Cross-cutting | Every SQL-/CRED-/LOG-/ERR-/MIG-/INIT- requirement from ARCHITECTURE.md §11.2 |
| **E2E** | `pytest` over `examples/` FastAPI app | Smoke of the developer-facing flow | Quickstart works; under-30-min outcome holds |
| **Benchmarks** | `criterion` (Rust), `pytest-benchmark` (Py) | Compile <1ms p99, hydrate <5ms p99/100 rows (ARCHITECTURE.md §14) | Performance budgets; not a blocking gate (tracked, regressions flagged) |

### 6.2 PyO3 Panic & Cancellation Tests (named, required)

- **Panic injection:** a test-only path forces a Rust panic across the boundary; asserts it surfaces as a catchable `FerrumInternalError` with no addresses/paths (ERR-2).
- **Cancellation:** asserts `asyncio` cancellation/timeout at the await point yields `FerrumTimeoutError`, and that no cancellation logic exists inside Rust (ARCHITECTURE.md §6.2).

### 6.3 Security Qualification Suite (release gate)

A dedicated `tests/python/security/` package, run as a required CI job, with one or more tests per requirement ID in ARCHITECTURE.md §11.2:

| Requirement IDs | Representative assertion |
|-----------------|--------------------------|
| SQL-1/2/3 | Unknown field → compile error, `sql_text` never contains user input; no `extra()` exists |
| CRED-1 | Fixture DSN/password never appears in any hook payload, exception, or log capture |
| LOG-1/2 | Default hook payload keys ⊆ Tier A allowlist; validation errors do not echo submitted values |
| ERR-1/2 | SQLSTATE → taxonomy mapping table; panic → catchable, sanitized |
| MIG-1/2/5/6/7/8 | Apply without dry-run fails; drop without confirm fails; unscoped `delete()` fails; token replay after apply fails; token never in argv/public logs |
| INIT-1/2 | Generated compose pins `127.0.0.1`; generated `.gitignore` excludes `.env`; init refuses writes outside cwd allowlist |

This suite gates release qualification and **must** be green before any tagged release. SecurityEngineer owns review of its completeness.

### 6.4 Architecture-Fitness Checks

- **Import boundary (Python):** `import-linter` contract asserts `ferrum.cli` / `ferrum.contrib` are not imported by core query modules, and core never imports framework packages.
- **Crate boundary (Rust):** CI asserts only `ferrum-pyo3` declares a `pyo3` dependency; `ferrum-core`/`ferrum-sql` must compile with `--no-default-features` and contain no `pyo3`/`tokio` (`cargo tree`/`cargo-deny` check).

---

## 7. CI/CD Pipeline Structure

GitHub Actions under `.github/workflows/`. Three workflows: fast PR checks, the release wheel build, and a scheduled drift check. Jobs fail fast and run independent concerns in parallel (Blast Radius: a lint failure should not mask a test failure).

### 7.1 `ci.yml` — Pull Request / push gate

Triggered on PR and push to `main`. Jobs (parallel unless a dependency is noted):

| Job | Runs | Tooling | Gate |
|-----|------|---------|------|
| `lint-python` | ruff check + ruff format --check | `ruff` | Required |
| `type-python` | strict type check | `mypy` (strict) on `python/ferrum` | Required |
| `lint-rust` | `cargo fmt --check` + `cargo clippy -D warnings` | `rustfmt`, `clippy` | Required |
| `check-rust` | `cargo check --workspace` + boundary check (§6.4) | `cargo`, `cargo-deny` | Required |
| `test-rust` | `cargo test --workspace` (incl. proptest) | `cargo test` | Required |
| `import-boundary` | import-linter contracts | `import-linter` | Required |
| `test-python` | build extension (debug) + pytest unit/property/integration | `maturin develop`, `pytest`, `hypothesis`, ephemeral PostgreSQL service | Required (matrix, §7.2) |
| `security-qual` | `pytest -m security` against ephemeral PG | `pytest` | Required (release gate) |
| `docs-check` | public-API-change → docs diff present; link check | script | Required |

PostgreSQL for `test-python`/`security-qual` is provided as a GitHub Actions **service container** (`postgres:16`), bound to localhost in the runner — no external DB.

### 7.2 Test Build Matrix (PR gate)

Concrete and implementable. Debug builds for speed on PRs; release builds reserved for `release.yml`.

| Axis | Values (v0.1) |
|------|---------------|
| OS | `ubuntu-latest` (primary), `macos-latest` (arm64) |
| Python | `3.11`, `3.12`, `3.13` |
| Rust | pinned channel from `rust-toolchain.toml` (single version) |
| Profile | `debug` on PR; `release` smoke on one cell (ubuntu + py3.12) per PR |

- Full OS×Python cross-product runs on `ubuntu` for every Python minor; `macos-latest` runs a reduced set (py3.11 + py3.13) to bound cost — exhaustive macOS coverage happens in `release.yml`.
- Windows is **not** in the test matrix (sdist-only target, ADR-005); a `windows-latest` sdist-build smoke runs in `release.yml` only.

### 7.3 `release.yml` — Wheel build & publish

Triggered on a version tag (`v*`). Implements ADR-005 packaging.

| Stage | Tooling | Output |
|-------|---------|--------|
| Build wheels | `cibuildwheel` + `maturin` | abi3 wheels: Linux manylinux x86_64 & aarch64; macOS arm64 & x86_64 |
| Build sdist | `maturin sdist` | source dist (Rust toolchain required to install on unmatched platforms incl. Windows) |
| Smoke-install | per-artifact install + `pytest -m smoke` | each wheel imports `ferrum`, runs a trivial compile+hydrate against PG |
| Security gate | rerun `pytest -m security` on a built wheel (not just dev build) | confirms qualification holds for the shipped artifact |
| Publish | trusted publishing (OIDC) to PyPI | gated on all prior stages + a manual release approval environment |

**Secrets posture (Defense in Depth):** publish uses PyPI Trusted Publishing (OIDC), so no long-lived PyPI token lives in CI. The ADR-007 migration confirmation token is never produced or consumed in CI workflows in this repo; any future deploy pipeline that applies migrations injects it via a secret channel (env/stdin), never argv or logs (MIG-7/8).

### 7.4 `nightly.yml` — Scheduled drift & cost control

- Full matrix incl. all macOS×Python cells and the Windows sdist smoke.
- `cargo-deny` (advisories/licenses) + `pip-audit` for dependency CVEs.
- Benchmark run (`criterion` + `pytest-benchmark`) with regression reporting (non-blocking; opens an issue on >X% regression).

---

## 8. Developer Tooling

| Concern | Tool | Config location | Notes |
|---------|------|-----------------|-------|
| Build (extension) | `maturin` | `pyproject.toml` `[build-system]` | `maturin develop` for local; `maturin build` for wheels |
| Python lint+format | `ruff` | `pyproject.toml` `[tool.ruff]` | replaces flake8/black/isort |
| Python types | `mypy` (strict) | `pyproject.toml` `[tool.mypy]` | `py.typed` shipped; `_native.pyi` stub checked |
| Python tests | `pytest` (+`pytest-asyncio`, `hypothesis`, `pytest-benchmark`) | `pyproject.toml` `[tool.pytest.ini_options]` | markers: `integration`, `security`, `smoke` |
| Rust format | `rustfmt` | `rustfmt.toml` | |
| Rust lint | `clippy` (`-D warnings`) | `Cargo.toml` lints | |
| Rust tests | `cargo test` + `proptest` | in-crate | |
| Rust benches | `criterion` | `benches/` | |
| Dependency policy | `cargo-deny`, `pip-audit` | `deny.toml` | advisories + license allowlist |
| Import boundaries | `import-linter` | `pyproject.toml`/`.importlinter` | enforces §6.4 contracts |
| Local orchestration | `Makefile` / `justfile` | repo root | `make test`, `make lint`, `make ci-local` mirror CI jobs |
| Pre-commit | `pre-commit` | `.pre-commit-config.yaml` | ruff + rustfmt + clippy fast subset |

### 8.1 Smallest-Verification Convention

Mirroring AGENTS.md §7, the local commands map to the smallest proof of a change:

- Rust-only change: `cargo test -p <crate>` + `cargo clippy -p <crate>`.
- Python-only change: `maturin develop && pytest tests/python/<area>`.
- Boundary/extension change: `maturin develop && pytest -m "integration or security"`.
- Full local CI parity: `make ci-local` (runs every required PR gate).

---

## 9. Security Requirements for Engineers (structure-level)

| Requirement | Where enforced in this structure |
|-------------|----------------------------------|
| Pure/sync Rust core | `ferrum-core`/`ferrum-sql` forbid `pyo3`/`tokio`; CI boundary check (§6.4) |
| Panic safety | `ferrum-pyo3` catch wrapper; panic-injection test (§6.2) |
| No bound values in default observability | Tier A payload-allowlist test in `security-qual` (LOG-1) |
| No secrets in CI | Trusted Publishing (OIDC); no migration token in repo workflows (§7.3) |
| Migration safety | `migrations/` module ownership + `security-qual` MIG tests; SecurityEngineer review required |
| init scaffold safety | `cli/init.py` + INIT-1/2 tests (§6.3) |
| Reproducible builds | pinned `rust-toolchain.toml`, committed `Cargo.lock`, pinned CI action versions |

**SecurityEngineer notification:** the `connection`, `hooks`, `errors`, and `migrations/*` modules, the `ferrum-pyo3` crate, and the `security-qual` CI job require SecurityEngineer review before v0.1 release qualification.

---

## 10. Failure Modes of the Structure Itself

| Risk | Consequence | Mitigation |
|------|-------------|------------|
| Wheel matrix cost creep | Slow/expensive CI (cost escalation) | Reduced PR matrix; full matrix only nightly/release; ADR-005 CEO cost gate |
| Extension/stub drift (`_native.pyi`) | Type checks pass but runtime breaks | `mypy` checks stub against a generated reference in CI; integration tests exercise the real extension |
| Crate-split churn if ADR-002 differs | Rework of `ferrum-sql` boundary | Split is internal-only; Python API and wheel unaffected; flagged for DECISIONS.md |
| Security suite rot | Gate passes while a control regresses | One test per requirement ID; SecurityEngineer owns completeness review per release |
| Single-distribution coupling | CLI/contrib deps bloat core | Optional extras + import-linter boundary (§4.2, §6.4) |

---

## 11. Open Items & Handoff

| Item | Owner | Blocks implementation? |
|------|-------|------------------------|
| CEO approval of this structure (+ ADR-005 cost) | CEO | Wheel/CI matrix scope only; dev builds can start |
| `ferrum-sql` vs in-`core` module decision | ChiefArchitect → `DECISIONS.md` (GUY-74) | No (internal; Python API stable) |
| Single-distribution vs split distributions | ChiefArchitect → `DECISIONS.md` (GUY-74) | No |
| CLI UX library choice (`typer` vs argparse) | Backend Engineer | No (base CLI works on stdlib) |
| `DATA_MODELING.md` (GUY-70) | ChiefArchitect | Yes for model implementation |
| SecurityEngineer review of security-qual completeness | SecurityEngineer | Yes for release qualification |

### 11.1 What Engineers Can Start After Approval

1. Scaffold the Cargo workspace (`crates/ferrum-core`, `ferrum-sql`, `ferrum-pyo3`) with empty modules + `cargo test` skeletons.
2. Scaffold `python/ferrum/` package skeleton with `py.typed`, `_native.pyi`, and `pyproject.toml` maturin config.
3. Stand up `ci.yml` with the §7.1 jobs (initially allowed to be near-empty but wired) so every later PR is gated.
4. Implement in the order recommended by ARCHITECTURE.md §17 (core IR + compile tests first).

---

*Produced by Chief Architect for GUY-71. Subordinate to ARCHITECTURE.md (GUY-69). Approved via CEO board approval `e69b1ccb` (2026-06-13). No production source created by this task.*
