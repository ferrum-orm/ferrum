---
task_id: w0-c-reproducible-baselines
wave: wave-0
owner: production-readiness-executor
status: verified
run_id: 20260821T090200Z
shared_path_lease: null
dependencies: []
owned_paths:
  - mise.toml
  - benchmarks/
  - tests/benchmarks/
  - .github/workflows/nightly.yml
security_triage_complete: true
security_surfaces:
  sql_compilation: false
  migration_apply: false
  errors_redaction: false
  auth_secrets: false
  rls_admin_gucs: false
  schema_selection: false
security_review: false
security_review_justification: benchmark and CI harness changes add no gated implementation
architecture_review: false
product_review: false
code_review: true
---

# Task: Reproducible baselines

## Specify

### Problem

Production-readiness changes lack stable live-PostgreSQL contract, wheel-smoke,
and end-to-end performance baselines.

### Pre-existing orchestration change

Bootstrap runs `20260821T074435Z`, `20260821T074737Z`, and
`20260821T075533Z` already added the `agent-orchestration` task and wired it into
`ci-local`. Load those logs and preserve that gate while extending `mise.toml`.

### Scope

Add deterministic task entrypoints and baseline artifacts for correctness,
latency, resource use, and compiler-free installation.

### Non-goals

No performance optimization and no hard regression threshold before variance is measured.

### Invariants and failure modes

Keep benchmarks comparable across runs, isolate setup from measured work, report
environment metadata, avoid secret output, and prevent flaky nightly noise from
blocking normal CI.

### Acceptance criteria

- Stable commands run consumer contracts, wheel smoke, and end-to-end benchmarks.
- Baselines include p50/p95/p99 plus environment metadata.
- Repeated local runs quantify variance.

## Plan

Reuse existing mise, compose, benchmark, and nightly patterns; add the smallest
missing harnesses; record baseline output in machine-readable form.

## Tasks

1. Audit existing tasks and benchmark harnesses.
2. Define deterministic datasets and warmup/measurement phases.
3. Add mise entrypoints and machine-readable reports.
4. Add non-blocking nightly execution.
5. Run repeated baselines and document variance.

## Implement

Ready for assignment after confirming no active edits overlap `mise.toml` or nightly CI.

## Validation contract

Run each new command twice from a clean test database, inspect full output, and
run `mise run ci-local`.

## Independent verification contract

Verifier reproduces commands without executor-local state and checks reported
statistics against raw samples.

## Revert contract

Remove only baseline harness/task/workflow entries added by this workstream.
Preserve the pre-existing orchestration task and existing CI.
