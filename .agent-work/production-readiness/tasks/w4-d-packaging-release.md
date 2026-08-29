---
task_id: w4-d-packaging-release
wave: wave-4
owner: production-readiness-executor
status: in_progress
run_id: 20260829T093132Z
shared_path_lease: w4-d-shared-20260829T093132Z
dependencies:
  - w1-e-pool-lifecycle
owned_paths:
  - tests/python/unit/test_packaging.py
  - tests/python/integration/test_wheel_smoke.py
  - .github/workflows/release.yml
security_triage_complete: true
security_surfaces:
  sql_compilation: false
  migration_apply: false
  errors_redaction: false
  auth_secrets: false
  rls_admin_gucs: false
  schema_selection: false
security_review: true
security_review_justification: Supply chain security — cargo audit, dependency review, SBOM/provenance, secret scanning, artifact signing
architecture_review: true
product_review: true
code_review: true
---

# Task: Packaging, supply chain, and release policy

## Specify

### Problem

abi3 wheels need install-testing without Rust/compiler fallback on Python 3.14
slim Linux. Required manylinux architectures and macOS arm64 need coverage.
sdist should be tested deliberately. Supply chain security (cargo audit,
dependency review, SBOM/provenance, artifact signing/attestation, license checks,
secret scanning) needs implementation. Supported PostgreSQL/Python versions,
upgrade/deprecation policy, security reporting, release candidates,
rollback/yank procedure, and changelog ordering need definition.

### Scope

`.github/workflows/release.yml` (shared path — lease granted), packaging tests,
and release policy documentation (in the log — do NOT edit README/CHANGELOG).
Test abi3 wheel installation on Python 3.14. Test sdist deliberately. Run supply
chain checks.

### Non-goals

No `pyproject.toml` / `Cargo.toml` edits (shared paths — NOT leased; record
findings in the log). No `__init__.py` edits (shared path, no lease). No
`README.md` / `CHANGELOG.md` edits (record policy in the log). No `uv.lock` edits.
No `mise.toml` edits. No performance benchmarks (W4-B).

### Invariants and failure modes

abi3 wheels install without Rust/compiler on Python 3.14 slim Linux. Wheel smoke
tests fail if `uv` silently compiles from source. sdist tested deliberately.
Supply chain: cargo audit, dependency review, SBOM/provenance, artifact
signing/attestation, license checks, secret scanning. Release policy: supported
PostgreSQL/Python versions, upgrade/deprecation, security reporting, RCs,
rollback/yank, changelog ordering.

### Acceptance criteria

- Build and install-test abi3 wheels without Rust/compiler on Python 3.14 slim
  Linux (or document that the CI environment doesn't support this and provide
  the workflow configuration for it).
- Cover required manylinux architectures and macOS arm64 (in the release.yml
  workflow).
- Test sdist deliberately.
- Fail wheel smoke tests if `uv` silently compiles from source.
- Run `cargo audit` (or document if not available in the environment).
- Define supported PostgreSQL/Python versions, upgrade/deprecation policy,
  security reporting, RCs, rollback/yank procedure, changelog ordering (in the
  log — do NOT edit README/CHANGELOG).
- Preserve existing OIDC trusted publishing in release.yml.

## Plan

Harden `release.yml` for Python 3.14 abi3 wheel coverage. Add wheel smoke tests.
Run cargo audit and supply chain checks. Document release policy in the log.
ChiefArchitect for the release architecture; SecurityEngineer for supply chain
security; ProductManager for supported versions/policy; CodeReviewer required.

## Tasks

1. Audit existing `release.yml` and identify gaps vs the plan.
2. Add Python 3.14 abi3 wheel build and install-test.
3. Add manylinux/macOS arm64 coverage (if not already present).
4. Add sdist test.
5. Add wheel smoke test that fails on silent source compilation.
6. Run `cargo audit` (or document if unavailable).
7. Document release policy (supported versions, deprecation, security reporting,
   RCs, rollback/yank, changelog ordering) in the log.
8. Preserve existing OIDC trusted publishing.
9. Focused checks plus `mise run ci-local`.

## Implement

Coordinator marked `in_progress` at `20260829T093132Z` with exclusive owned paths
and lease `w4-d-shared-20260829T093132Z` for `.github/workflows/release.yml`.
Implement the Tasks section.

## Validation contract

Wheel smoke tests, sdist test, cargo audit, then `mise run ci-local`.

## Independent verification contract

Verifier proves abi3 wheel install, sdist test, supply chain checks, and release
policy documentation. Named gates: ChiefArchitect, SecurityEngineer,
ProductManager, CodeReviewer — all required.

## Revert contract

Revert only owned release.yml/test files from this run. Preserve all other
workstreams and existing CI workflows.
