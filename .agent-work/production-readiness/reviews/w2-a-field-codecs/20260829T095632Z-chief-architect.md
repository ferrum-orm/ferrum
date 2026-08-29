---
task_id: w2-a-field-codecs
run_id: 20260829T095632Z
authority: ChiefArchitect
reviewer: chief-architect-agent
reviewed_at: 2026-08-29T12:00:00Z
base_revision: 612f476c32fa7b1fbd38e4dc9f4c689d05b72191
decision: approved
scope:
  - python/ferrum/models.py
  - python/ferrum/registry.py
  - crates/ferrum-core/src/ir/metadata.rs
---

# Named Authority Verdict

## Authority

ChiefArchitect

## Claims reviewed

1. **FieldCodec protocol architecture** — typed `encode_bind`/`decode_result`/`redact`
   contract is Python-side only, pure functions, no I/O, no async, no Rust core
   involvement (§2.1, §2.3, §4).
2. **Codec registration design** — factory registry pattern in `registry.py` with
   `register_codec_factory`/`get_codec_factory`; factories are callables, not
   instances; no key material stored in the registry.
3. **Encrypted codec key-provider injection architecture** — `KeyProvider` Protocol
   with `get_key(key_id) -> bytes` / `key_ids() -> tuple[str, ...]`; keys injected at
   codec construction time, never in model metadata; key rotation by changing the
   provider, not the model (§3 credential handling).
4. **Nested type hydration design** — `NestedModelCodec`/`NestedListModelCodec` use
   `model_construct` (ADR-003 trusted DB fast path); model class resolved from
   Ferrum registry at construction time.
5. **Rust type metadata extension** — `Citext`, `Inet`, `Domain` variants added to
   `FieldType` enum; codec is Python-only; `domain_name` stored Python-side only,
   not sent across the PyO3 boundary (§4).
6. **Boundary compliance** — `to_metadata_dict()` structurally excludes `codec_meta`,
   `codec_pii`, `codec_key_id`, `codec_domain`, `domain_name`, `codec_model` from
   the Rust boundary payload; only `field_type` (possibly overridden by codec kind)
   is sent for SQL casting (§4, §2.9).
7. **Immutability** — `CodecMeta` is `@dataclasses.dataclass(frozen=True)`; model
   metadata built once at class-definition time (§2.10).

## Evidence

### Source inspection

`git diff HEAD -- python/ferrum/models.py python/ferrum/registry.py crates/ferrum-core/src/ir/metadata.rs`:

- `models.py` (+829 lines): `CodecMeta` frozen dataclass (`frozen=True`), `KeyProvider`
  Protocol, `FieldCodec` Protocol (pure `encode_bind`/`decode_result`/`redact`, no
  async), `_encrypt`/`_decrypt` primitives (encrypt-then-MAC), 7 concrete codecs,
  `_CODEC_FIELD_TYPE_OVERRIDES` mapping, factory registration via
  `_register_default_codecs()`, `codec_meta`/`domain_name` on `FieldMeta`,
  6 optional `codec_*` parameters on `Field()`.
- `registry.py` (+67/-3): `_CODEC_FACTORIES` dict, `register_codec_factory`,
  `get_codec_factory` (raises `FerrumCompileError` on unknown kind), `all_codec_kinds`,
  `clear_codec_registry_for_tests`.
- `metadata.rs` (+6): `Citext`, `Inet`, `Domain` enum variants with doc comments.

### Boundary exclusion (§4)

`to_metadata_dict()` at `models.py:351-396` explicitly enumerates field payload keys:
`name`, `column_name`, `field_type`, `allowed_operators`, `nullable`, plus optional
`vector_dimensions`/`fts_config`/`fts_source_columns`/`jsonb_list`/`generated`/`read_only`.
It does NOT emit `codec_meta`, `codec_pii`, `codec_key_id`, `codec_domain`,
`domain_name`, or `codec_model`. This is structural (explicit key list, not
field-iteration), so the new `FieldMeta` dataclass fields are excluded by
construction. Verified by reading the source directly.

### Architectural constraint conformance

| §2 constraint | Status | Evidence |
|---|---|---|
| §2.1 Python owns public ergonomics | ✓ | `FieldCodec`, `CodecMeta`, `KeyProvider`, factories all in Python |
| §2.2 Rust owns perf internals only | ✓ | Codec is Python-only; Rust gets only 3 enum variants for type casting |
| §2.3 Async-first, no sync wrapper | ✓ | Codec methods are pure sync transforms (not I/O), correctly non-async |
| §2.4 Pydantic v2 first | ✓ | Nested codecs use `model_dump(mode="json")` / `model_construct` |
| §2.5 PyO3 boundary maps errors | ✓ | No Rust error paths added; codec errors are Python `FerrumCodecError` |
| §2.9 No raw SQL escape | ✓ | No SQL introduced; codec field types feed existing allowlist compilation |
| §2.10 No per-request mutable shared state | ✓ | `CodecMeta` frozen; built at class-definition time; runtime codec resolved per-query by W2-B |

### Unit tests

```
$ uv run pytest tests/python/unit/test_field_codecs.py -x -q
177 passed in 0.34s
```

### Lint / format / type (owned files)

```
$ uv run ruff format --check python/ferrum/models.py python/ferrum/registry.py
4 files already formatted
$ uv run ruff check python/ferrum/models.py python/ferrum/registry.py
All checks passed!
$ uv run ty check python/ferrum/models.py python/ferrum/registry.py
All checks passed!
$ cargo clippy -p ferrum-core -- -D warnings
Finished (no warnings)
```

## Findings

| # | Severity | Finding | Required correction |
|---|----------|---------|---------------------|
| 1 | Info | `VectorCodec` factory (`_make_simple_factory`) constructs `VectorCodec(meta)` without passing `dimensions`. `CodecMeta` has no `dimensions` field; dimensions live on `FieldMeta.vector_dimensions`. Dimension validation at encode time is skipped when constructed via the factory. | W2-B (which wires codecs into queryset) must pass `dimensions` from `FieldMeta.vector_dimensions` when constructing `VectorCodec`, or `CodecMeta` should carry a `dimensions` field. Non-blocking for W2-A — the codec contract is defined but not wired. |
| 2 | Info | `_register_default_codecs()` executes at module import time (side effect). This follows the existing `register_model` pattern but is a import-time mutation of global registry state. | Acceptable for alpha; follows existing pattern. If import-time side effects become a concern, wrap in an idempotent guard. |

No architecture-level blockers found. The immutable `CodecMeta` / runtime `FieldCodec`
separation is the correct design: it keeps model metadata read-only (§2.10), allows
key rotation without model redefinition, and cleanly defers hydration wiring to W2-B.

## Decision

`approved`

The FieldCodec protocol architecture, codec registration design, encrypted codec
key-provider injection, nested type hydration (ADR-003 `model_construct`), and Rust
type metadata extension all conform to §2, §3, and §4. The boundary payload
structurally excludes codec metadata. No undecided ADR is pre-empted. The two info
findings are W2-B follow-ups, not W2-A blockers.
