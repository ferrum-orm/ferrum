---
task_id: w2-a-field-codecs
wave: wave-2
owner: production-readiness-executor
status: in_progress
run_id: 20260829T095632Z
shared_path_lease: null
dependencies:
  - w1-f-tenancy-shards
owned_paths:
  - python/ferrum/models.py
  - python/ferrum/registry.py
  - crates/ferrum-core/src/ir/metadata.rs
  - tests/python/unit/test_field_codecs.py
  - tests/python/integration/test_field_codecs.py
security_triage_complete: true
security_surfaces:
  sql_compilation: false
  migration_apply: false
  errors_redaction: false
  auth_secrets: false
  rls_admin_gucs: false
  schema_selection: false
security_review: true
security_review_justification: Encrypted codecs with key-provider injection and PII redaction are security-gated
architecture_review: true
product_review: false
code_review: true
---

# Task: Typed field codecs and PostgreSQL types

## Specify

### Problem

There is no typed Python-side `FieldCodec` contract for bind/result conversion. Nested
Pydantic models/lists stored in JSONB, encrypted string/JSON codecs with key-provider
injection, citext, inet, bytea, arrays, enums, vector dimensions, and custom PostgreSQL
domains are not systematically supported. Codec metadata is not immutable, IDE-visible,
migration-aware, or excluded from logs/hooks/errors.

### Scope

`python/ferrum/models.py` (FieldCodec contract, codec registration, type metadata),
`python/ferrum/registry.py` (codec registry), `crates/ferrum-core/src/ir/metadata.rs`
(Rust type metadata), and owned tests. Do NOT edit `queryset.py` (W2-B owns) —
hydration integration is a follow-up after W2-B/C complete.

### Non-goals

No `queryset.py` edits (W2-B owns — overlap risk). No `__init__.py` edits (shared path,
no lease). No `README.md` / `CHANGELOG.md` (record bullets in log). No `hooks.py` /
`observability.py` edits (W4-A owns, complete). No `errors.py` edits (W1-D owns, complete).

### Invariants and failure modes

FieldCodec contract is typed, immutable, IDE-visible, migration-aware. Encrypted codecs
use key-provider injection (no hardcoded keys). PII redaction in logs/hooks/errors.
Hydration constructs declared nested types correctly despite trusted DB fast path.
Codec metadata excluded from logs/hooks/errors. Deterministic and randomized round-trip
tests, key rotation/failure tests, malformed ciphertext behavior, PII redaction tests.

### Acceptance criteria

- Typed `FieldCodec` contract for bind/result conversion without moving I/O into Rust.
- Support nested Pydantic models/lists stored in JSONB.
- Encrypted string/JSON codecs with key-provider injection.
- citext, inet, bytea, arrays, enums, vector dimensions, custom PostgreSQL domains.
- Hydration constructs declared nested types correctly.
- Codec metadata immutable, IDE-visible, migration-aware, excluded from logs/hooks/errors.
- Deterministic and randomized round-trip tests.
- Key rotation/failure tests, malformed ciphertext behavior, PII redaction tests.

## Plan

Add `FieldCodec` protocol/ABC in `models.py`. Add codec registration in `registry.py`.
Update Rust type metadata in `metadata.rs`. ChiefArchitect for the codec architecture;
SecurityEngineer for encrypted codecs and PII redaction; CodeReviewer required.

## Tasks

1. Audit existing `models.py` and `registry.py` and identify gaps vs the plan.
2. Add typed `FieldCodec` contract (bind/result conversion, no I/O).
3. Add nested Pydantic model/list JSONB codec.
4. Add encrypted string/JSON codec with key-provider injection.
5. Add citext, inet, bytea, array, enum, vector dimension, custom domain codecs.
6. Update Rust type metadata in `metadata.rs` if needed.
7. Ensure codec metadata is immutable, IDE-visible, migration-aware, excluded from logs.
8. Deterministic and randomized round-trip tests.
9. Key rotation/failure, malformed ciphertext, PII redaction tests.
10. Focused checks plus `mise run ci-local`.

## Implement

Coordinator marked `in_progress` at `20260829T095632Z` with exclusive owned paths and
no shared-path lease. Do NOT edit queryset.py — hydration integration is a follow-up.

## Validation contract

Focused codec unit tests plus live codec integration tests against PostgreSQL, then
`mise run ci-local`.

## Independent verification contract

Verifier proves codec round-trips, encrypted key rotation, PII redaction, and nested
type hydration. Named gates: ChiefArchitect, SecurityEngineer, CodeReviewer.
ProductManager `not_required`.

## Revert contract

Revert only owned models/registry/metadata/test files from this run. Preserve all
other workstreams.
