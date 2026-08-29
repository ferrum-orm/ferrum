---
task_id: w4-d-packaging-release
run_id: 20260829T093132Z
authority: ProductManager
reviewer: product-manager-agent
reviewed_at: 2026-08-29T12:00:00Z
base_revision: 87f39966d60303b30943308c9123418d9d47252e
decision: approved
scope:
  - release policy (supported PostgreSQL/Python versions, upgrade/deprecation, security reporting, RCs, rollback/yank, changelog ordering)
---

# Named Authority Verdict — ProductManager

## Authority

ProductManager

## Claims reviewed

1. Supported Python versions are defined and align with the abi3 contract.
2. Supported PostgreSQL versions are defined and align with §2.6 / resolution A.
3. Upgrade/deprecation policy is defined and aligns with §5a compatibility
   policy (alpha 0.x no semver, post-1.0 deprecation window).
4. Security reporting is defined (channel, SLA, supported versions for fixes).
5. Release candidates (RCs) are defined (trigger, workflow, stability).
6. Rollback/yank procedure is defined (yank vs delete, pin prior, revert
   migration, emergency patch).
7. Changelog ordering is defined (format, section order, version headers,
   security entries, breaking changes).

## Evidence

### Inspected paths

- `.agent-work/production-readiness/logs/w4-d-packaging-release/20260829T093132Z.md`
  lines 201-260 (Release policy section).
- `AGENTS.md` §5a (alpha-to-stable compatibility policy, ProductManager
  resolution A), §2.6 (PostgreSQL only production target).
- `pyproject.toml` (via test_packaging.py evidence): `requires-python = ">=3.11"`,
  classifiers list 3.11-3.14, `Development Status :: 3 - Alpha`, version 0.1.17.
- `tests/python/unit/test_packaging.py` (16 tests validating the policy
  invariants in code).

### Release policy assessment

**Supported Python versions** (log lines 203-209):
- Floor: CPython 3.11 (`requires-python = ">=3.11"`, `pyo3/abi3-py311`).
- Tested: 3.11, 3.12, 3.13, 3.14 (classifiers + CI build + smoke-install matrix).
- abi3 contract: single wheel built with abi3 minimum (3.11), installs on all
  3.11+. Correct — this is the product-appropriate contract for a PyO3/abi3
  extension.
- CPython only (no PyPy/GraalPy). Correct for v0.1.

**Supported PostgreSQL versions** (log lines 211-214):
- Production-readiness target: PostgreSQL 16 (CI `security-gate-wheel` uses
  `postgres:16`). Aligns with §2.6.
- Best-effort: PostgreSQL 13+ (asyncpg supports 13+). Reasonable.
- Other backends (MySQL, SQLite, MSSQL): best-effort thin parity (§2.6,
  resolution A). Not release gates. Correct — this is the ratified product
  scope.

**Upgrade/deprecation policy** (log lines 216-221):
- Alpha (0.x): no semver/stability contract. Breaking changes may land in any
  0.x release without deprecation or advance notice. IR version has already
  moved. **Aligns exactly with §5a ProductManager resolution A** (policy item
  1: "0.x / alpha: breaking changes are allowed in any 0.x release without
  deprecation or advance notice").
- After 1.0: deprecation window of at least one minor release and 90 days,
  whichever is longer, recorded in CHANGELOG.md and README.md. **Aligns with
  §5a policy item 1**.
- IR version: internal PyO3 contract, not user-facing. After 1.0, IR major
  bump is a Ferrum major-version event. **Aligns with §5a policy item 2**.
- Generated SQL text: never a compatibility surface. **Aligns with §5a policy
  item 3**.

**Security reporting** (log lines 224-228):
- Private reporting via GitHub Security Advisories (not public issues).
  Industry-standard channel.
- Response SLA: 5 business days ack, 30 days fix for High/Critical. Reasonable
  for an alpha-stage open-source project.
- Security advisories published as GitHub Security Advisories with CVE
  assignment request when applicable. Correct.
- Supported versions for security fixes: only latest 0.x (alpha policy). After
  1.0, latest minor of current major. Aligns with the alpha compatibility
  policy.

**Release candidates (RCs)** (log lines 230-234):
- Optional, at maintainer's discretion for major/minor releases. Tagged
  `vX.Y.ZrcN`, published to TestPyPI (not PyPI). Correct — RCs are a
  product-appropriate testing channel.
- RC workflow not in current release.yml (which publishes to PyPI on `v*` tag
  push). A future RC workflow would add a `v*rc*` tag trigger targeting
  TestPyPI. Reasonable follow-up.
- RCs are for testing only, not production-supported. Correct.

**Rollback/yank procedure** (log lines 236-241):
- Yank (not delete) via PyPI web UI or twine. Removes from simple index, keeps
  record for installed dependents. Correct — yank is the PyPI-appropriate
  action for a broken release.
- Rollback to prior version: pin consumers to last known-good. abi3 wheel
  remains on PyPI. Correct.
- Revert migration code: pin prior Ferrum version + revert migration ledger
  entry (Django-style `ferrum revert`). Per-database ledger, no global
  rollback. Correct — aligns with the migration design.
- Emergency: cut a patch release (`vX.Y.Z+1`). Do not re-push broken tag
  (force-push tags disabled). Correct.

**Changelog ordering** (log lines 244-250):
- Format: Keep a Changelog (1.1.0) sections: Added, Changed, Deprecated,
  Removed, Fixed, Security. Industry-standard.
- Entry order: most recent first (reverse-chronological) within a section.
- Version headers: `## [X.Y.Z] — YYYY-MM-DD` (Unreleased section at top).
- Security entries in Security section (not Fixed), even if the fix is a bug.
  Correct.
- Breaking changes tagged `BREAKING:` prefix, referenced in 0.x→next migration
  notes. Correct.
- Releases in reverse-chronological order. release.yml does NOT auto-generate
  changelog entries — maintainer edits CHANGELOG.md before tagging. Correct
  (manual changelog is appropriate for v0.1).

**Supply chain security policy** (log lines 252-260):
- cargo audit, dependency review, SBOM/provenance, PyPI attestations, license,
  secret scanning, artifact signing all documented. Aligns with the W4-D task
  contract acceptance criteria.

### Policy documentation location

The policy is documented in the executor log (lines 201-260), NOT in
README.md / CHANGELOG.md. This is correct per the task contract (those are
shared paths without leases). The policy should be migrated to README.md /
CHANGELOG.md in a future task when those paths are leased. This is a
documentation follow-up, not a product defect.

## Findings

| # | Severity | Evidence | Required correction |
|---|---|---|---|
| F1 | Follow-up | Release policy is in the log, not README/CHANGELOG (shared paths without leases) | Future task: migrate policy to README.md / CHANGELOG.md when those paths are leased. Not a W4-D defect. |
| F2 | Follow-up | RC workflow not in release.yml (only `v*` → PyPI) | Future task: add `v*rc*` → TestPyPI workflow when RC process is formalized. Documented in policy. |

## Decision

`approved`

The release policy is complete, product-appropriate, and aligns with §5a
alpha-to-stable compatibility policy (ProductManager resolution A) and §2.6
PostgreSQL-only production target. Supported versions, upgrade/deprecation,
security reporting, RCs, rollback/yank, and changelog ordering are all
defined with correct product semantics. The policy is documented in the log
(correct — README/CHANGELOG are shared paths without leases). The two
follow-ups (migrate policy to README/CHANGELOG; add RC workflow) are future
tasks, not W4-D defects.

This record grants only the ProductManager gate. It does not substitute for
another authority or independent verification.
