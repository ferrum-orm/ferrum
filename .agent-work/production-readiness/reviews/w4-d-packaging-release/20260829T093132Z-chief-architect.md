---
task_id: w4-d-packaging-release
run_id: 20260829T093132Z
authority: ChiefArchitect
reviewer: chief-architect-agent
reviewed_at: 2026-08-29T12:00:00Z
base_revision: 87f39966d60303b30943308c9123418d9d47252e
decision: approved
scope:
  - release architecture (abi3 wheels, Python 3.14, manylinux/macOS, sdist, OIDC, build provenance/attestations)
---

# Named Authority Verdict — ChiefArchitect

## Authority

ChiefArchitect

## Claims reviewed

1. abi3 wheel architecture preserved and extended to Python 3.14 (ADR-005).
2. Required manylinux architectures (x86_64, aarch64) and macOS arm64 covered.
3. sdist tested deliberately on Linux.
4. OIDC trusted publishing preserved.
5. Build provenance attestations added for wheels and sdist.
6. PyPI sigstore attestations added.
7. Smoke-install guards against silent source compilation.
8. No architecture invariants (§2) violated by the release.yml changes.

## Evidence

### Inspected paths

- `git diff HEAD -- .github/workflows/release.yml` (95 lines changed, +90/-5).
- `.agent-work/production-readiness/logs/w4-d-packaging-release/20260829T093132Z.md`.
- `.agent-work/production-readiness/verification/w4-d-packaging-release/20260829T093132Z.md`.
- `AGENTS.md` §2 (non-negotiable constraints), §5 (ADR-005), §8 (definition of done).
- `tests/python/unit/test_packaging.py`, `tests/python/integration/test_wheel_smoke.py`.

### ADR-005 compliance

ADR-005 ratifies: maturin + cibuildwheel abi3 wheels; `release.yml` builds and
publishes to PyPI on `v*` tag push via OIDC trusted publishing. The diff preserves
every element of ADR-005 and adds supply-chain verifiability:

- `CIBW_BUILD: "cp311-* cp312-* cp313-* cp314-*"` (release.yml:53). The
  `pyo3/abi3-py311` feature (tool.maturin) produces a single `cp311-abi3-*` wheel
  per platform that installs on CPython 3.11+. The extra `cp314-*` selector
  exercises the 3.14 toolchain in CI to catch version-specific build regressions;
  the output wheel tag remains abi3. If cibuildwheel v4.1.0 lacks a cp314
  manylinux image, that selector skips non-fatally; the cp311 abi3 wheel still
  installs on 3.14 (proven by the smoke-install matrix). Architecturally sound.
- `smoke-install` matrix `python-version: ["3.11", "3.12", "3.13", "3.14"]`
  (release.yml:177). Proves the abi3 claim per minor version without a per-version
  wheel. Correct abi3 contract verification.
- manylinux x86_64 + aarch64 (native ARM runner `ubuntu-24.04-arm`), macOS arm64
  + x86_64 (cross-compile): present in matrix, unchanged from HEAD. ADR-005
  manylinux-only scope preserved (musllinux skip retained).
- `linux-sdist-smoke` (release.yml:144-163): new job installing from sdist,
  compiling the Rust extension, importing `ferrum`, printing `__version__`.
  Follows the existing `windows-sdist-smoke` pattern. Correct deliberate sdist
  test.
- OIDC trusted publishing preserved: `publish` job retains `environment: release`
  (release.yml:275), `permissions: id-token: write` (release.yml:277),
  `pypa/gh-action-pypi-publish@release/v1` (release.yml:283). `needs` correctly
  extended to include `linux-sdist-smoke` and `cargo-audit` (release.yml:273).
- Build provenance: `actions/attest-build-provenance@v1` for wheels
  (release.yml:85-87) and sdist (release.yml:108-110). Signed attestations
  verifiable via `gh attestation verify`. Correct supply-chain verifiability
  addition consistent with ADR-005's intent.
- PyPI sigstore attestations: `attestations: true` on
  `pypa/gh-action-pypi-publish@release/v1` (release.yml:286). OIDC-backed, no
  long-lived key. Correct.

### Silent source-compile guard architecture

`smoke-install` (release.yml:188-211) adds:
1. `Mask Rust toolchain` step: creates failing `cargo`/`rustc` stubs (exit 127)
   in `$HOME/norust/bin` prepended via `GITHUB_PATH`.
2. `Install wheel and fail on silent source compilation` step: captures
   `pip install --log pip-install.log` and greps for `Building wheel for
   ferrum-orm`.

This is a defense-in-depth architecture: PATH-mask stubs fail loudly if pip
invokes cargo/rustc, and the pip-log grep catches the sdist-compile signature.
The verifier noted a theoretical bypass via `RUSTUP_HOME`/`CARGO_HOME` env vars,
but the CI `smoke-install` job sets neither and `actions/setup-python` does not
install Rust. Not a practical concern in this workflow.

### §2 invariant check

- §2.5 (PyO3 + maturin bridge): preserved — no boundary changes.
- §2.6 (PostgreSQL only production target): the release.yml changes do not
  alter dialect scope; the `security-gate-wheel` job still uses `postgres:16`.
- §2.7 (no feature without tests): test_packaging.py (16 tests) and
  test_wheel_smoke.py (5 tests) cover the new invariants.
- §2.9 (no raw SQL escape hatches): not applicable to release.yml.
- §2.10 (no per-request mutable shared state in Rust): not applicable.

## Findings

| # | Severity | Evidence | Required correction |
|---|---|---|---|
| F1 | Minor | `release.yml:264` `cargo install cargo-audit --locked` is unpinned; v0.22.2 requires rustc 1.88, runner `stable` may be older → non-deterministic CI | Pin `--version 0.22.1` (or compatible) or use a prebuilt binary. Follow-up, not an architecture blocker. |
| F2 | Minor | `actions/attest-build-provenance@v1` requires repo-level artifact attestation feature enabled | Confirm repo setting before next release. Follow-up, not an architecture defect. |
| F3 | Non-blocking | `cp314-*` in `CIBW_BUILD` is redundant with `pyo3/abi3-py311` for the artifact tag but exercises the 3.14 toolchain | Acceptable as defensive coverage. Document the redundancy in the comment (already done at release.yml:49-52). |

## Decision

`approved`

The release architecture is sound, preserves ADR-005 in every element, and
adds appropriate supply-chain verifiability (build provenance + sigstore
attestations). The abi3 wheel contract is correctly extended to Python 3.14
via toolchain exercise + smoke-install matrix. The silent source-compile guard
is a well-structured defense-in-depth. No §2 architecture invariants are
violated. Minor follow-ups (cargo-audit version pin, repo attestation feature
enablement) are operational prerequisites, not architecture defects.

This record grants only the ChiefArchitect gate. It does not substitute for
another authority or independent verification.
