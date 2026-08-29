---
task_id: pilot-org-ai-platform
run_id: 20260829T104025Z
authority: SecurityEngineer
reviewer: security-engineer-agent
reviewed_at: 2026-08-29T10:47:02Z
base_revision: 768784ef788eb0641c224ead99d1e35662c3f8e3
decision: approved
scope:
  - Schema selection does not leak across tenants (search_path reset, identifier validation)
  - Shard routing does not leak across tenants (trusted keys, connection-explicit, PostgreSQL-only)
  - Encrypted codec key-provider injection and PII redaction
  - select_for_update compile-time rejection of invalid modifiers
---

# Named Authority Verdict

## Authority

SecurityEngineer.

## Claims reviewed

1. Schema selection (`schema_transaction`) cannot leak tenant state across
   pooled connections and cannot be used for SQL injection via the schema
   identifier (§3 SQL safety, §5a schema tenancy).
2. Shard routing (`ConnectionRegistry` / `ShardRouter`) cannot route to an
   unregistered or non-PostgreSQL connection and resolves only trusted
   caller-supplied keys (§5a sharding: "No implicit connection selection
   from model metadata, tenant id, or schema name").
3. Encrypted codecs inject keys via a `KeyProvider` at query time (never
   hardcoded in model metadata), use encrypt-then-MAC with a random nonce,
   fail closed on MAC mismatch, and `redact()` never exposes plaintext,
   ciphertext, or key material (§3 credential handling / PII redaction).
4. `select_for_update` rejects mutually exclusive modifiers at compile time
   before SQL emission (§3 SQL safety).

## Evidence

### Schema selection — no cross-tenant leak

- `python/ferrum/session.py:219-241` `_validate_schema_name`: identifier
  regex `^[a-zA-Z_][a-zA-Z0-9_]{0,62}$` enforced FIRST (raises
  `FerrumCompileError` category `invalid_identifier`), then allowlist
  (raises category `schema_not_allowed`). The injection test
  (`test_schema_transaction_rejects_injection_identifier`,
  `test_org_ai_platform_contracts.py:118`) proves `public; DROP TABLE x`
  is rejected by the regex **even when artificially added to the
  allowlist** — regex-first defense, §2.9 no-raw-SQL applied to schema
  selection. ✓
- `python/ferrum/session.py:113` `set_config` uses
  `set_config('name', $1, true)` — the `true` third arg makes the GUC
  **transaction-local**, guaranteeing reset on commit/rollback/cancellation
  (no `search_path` leakage onto a pooled connection).
- `test_schema_transaction_sets_and_resets_search_path`
  (`test_org_ai_platform_contracts.py:73`) asserts the transaction-local
  `search_path` contains the schema AND that the pooled connection's
  `search_path` no longer contains it after commit. Verified live against
  PostgreSQL (passed). ✓
- Schema identifier is bound as a **bound parameter** (`$1`), not
  interpolated (`session.py:113`), and the regex prevents any identifier
  containing `;`, spaces, or quotes from reaching `set_config`. No SQL
  injection path. ✓

### Shard routing — no cross-tenant leak

- `python/ferrum/routing.py:55-63` `_ensure_postgres_dsn`: rejects
  non-postgres schemes at registration time (`FerrumConfigError`). The
  test (`test_shard_router_registry_api_is_available`,
  `test_org_ai_platform_contracts.py:143`) proves `mysql://` is
  structurally rejected. Prevents a non-PostgreSQL pool silently
  coexisting with the PostgreSQL-only tenancy/RLS model. ✓
- `python/ferrum/routing.py:335-342` `connection_for`: calls the caller
  `resolver` (trusted key) then `registry.get(name)`. The router never
  inspects model metadata, tenant ids, or schema names — the resolver is
  the single routing-policy point (§5a). ✓
- `python/ferrum/routing.py:163-172` `get`: raises `FerrumConfigError`
  when the registry is closed or the name is unregistered.
  `test_shard_router_resolves_trusted_key_to_explicit_connection`
  (`test_org_ai_platform_contracts.py:163`) proves a bad resolver →
  `FerrumConfigError` and `close()` then `get()` → `FerrumConfigError`
  (no half-open registry state leaks). ✓
- Distinct trusted keys resolve to distinct connections (test asserts
  `conn_a is not conn_b`); the router does not pool-share across shards.
  ✓

### Encrypted codecs — key-provider + PII redaction

- `python/ferrum/models.py:585-612` `KeyProvider` protocol: keys are
  injected via `get_key(key_id)` at query time. The factory test
  (`test_encrypted_codec_factory_requires_key_provider_at_query_time`,
  `test_org_ai_platform_contracts.py:447`) proves
  `_make_encrypted_string_factory()` raises `FerrumCodecError` when no
  `key_provider` is supplied — keys are **never hardcoded in model
  metadata**. ✓
- `python/ferrum/models.py:688-760` encrypt-then-MAC: random 16-byte
  nonce (`secrets.token_bytes`), separate enc/mac keys derived via
  HMAC-SHA256, MAC verified with `hmac.compare_digest` (constant-time).
  The wrong-key test
  (`test_encrypted_codec_rejects_wrong_key_with_mac_failure`,
  `test_org_ai_platform_contracts.py:427`) proves decryption with the
  wrong key raises `FerrumCodecError` (never returns plaintext). ✓
- Non-deterministic ciphertext (random nonce per call) — asserted in both
  string and JSON round-trip tests. Prevents ciphertext-equality leakage. ✓
- `python/ferrum/models.py:950-951,994-995` `redact()` returns
  `[REDACTED:encrypted_*:key_id=...]` — only the non-secret `key_id`
  identifier. The redaction test
  (`test_encrypted_codec_redact_never_exposes_plaintext_or_key`,
  `test_org_ai_platform_contracts.py:460`) asserts plaintext, ciphertext
  fragment ("PII"), and key hex bytes never appear in the redacted
  representation. ✓ (§3 credential handling / PII redaction)

### select_for_update — compile-time rejection

- `python/ferrum/queryset.py:1837-1840`: `nowait=True, skip_locked=True`
  raises `FerrumCompileError` before SQL emission.
  `test_select_for_update_rejects_mutually_exclusive_modifiers`
  (`test_org_ai_platform_contracts.py:224`) proves this. ✓

### No source-code changes

- `git diff HEAD --name-only -- python/ crates/` → empty. No security-
  sensitive source code (SQL compilation, migration apply, errors,
  auth/secrets, RLS/admin GUCs, schema selection) was modified by this
  run. The security surfaces tagged in the task contract
  (`schema_selection: true`; all others false) are verified via **tests
  against existing implemented source**, not by editing source. ✓

### Fresh verification

```
FERRUM_TEST_DSN=postgresql://ferrum_test:ferrum_test@localhost:5432/ferrum_test \
  uv run pytest tests/consumer_contracts/test_org_ai_platform_contracts.py -q -m "" --tb=short
→ 18 passed in 0.34s (exit 0)
```

## Findings

- **Low — `.bench-results/perf.json` regenerated outside owned paths:** a
  tracked benchmark artifact rewritten by `mise run ci-local`. Not
  security-relevant (benchmark numbers only, no secrets/DSNs/PII).
  Revert before commit. The executor log omitted it from changed paths.
- No security findings. Schema selection is regex+allowlist+transaction-
  local-reset (no leak, no injection). Shard routing is trusted-key +
  connection-explicit + PostgreSQL-only (no implicit routing, no
  non-postgres pool). Encrypted codecs inject keys at query time,
  authenticate via constant-time MAC, and redact to a key-id placeholder.
  `select_for_update` rejects invalid modifiers at compile time. No
  Ferrum security-sensitive source was modified.

## Decision

**approved.** Schema selection and shard routing do not leak across
tenants. Encrypted codec key-provider injection and PII redaction are
verified. The `schema_selection` security surface tagged in the task
contract is cleared. No source-code changes to security-sensitive paths.

This record grants only the SecurityEngineer gate. It does not substitute
for another authority or independent verification.
