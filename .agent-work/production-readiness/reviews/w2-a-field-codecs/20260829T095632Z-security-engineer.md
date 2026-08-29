---
task_id: w2-a-field-codecs
run_id: 20260829T095632Z
authority: SecurityEngineer
reviewer: security-engineer-agent
reviewed_at: 2026-08-29T12:00:00Z
base_revision: 612f476c32fa7b1fbd38e4dc9f4c689d05b72191
decision: approved
scope:
  - python/ferrum/models.py
  - python/ferrum/registry.py
---

# Named Authority Verdict

## Authority

SecurityEngineer

## Claims reviewed

1. **No hardcoded keys** — encrypted codecs use `KeyProvider` injection; no key
   material in source, metadata, or payloads (§3 credential handling).
2. **PII redaction** — `redact()` never returns raw value, ciphertext, or key
   material; codec metadata excluded from logs/hooks/errors (§3 tiered
   observability, §3 error boundaries).
3. **Malformed ciphertext fails safely** — too-short, empty, tampered MAC, tampered
   ciphertext, wrong key, wrong-size key all fail with `FerrumCodecError` (§3 error
   boundaries).
4. **Key rotation works** — encrypt with key A, decrypt with A, re-encrypt with B,
   decrypt with B (§3 credential handling).
5. **Authenticated encryption construction** — encrypt-then-MAC, random nonce,
   constant-time MAC comparison, key-size enforcement.

## Evidence

### No hardcoded keys (C1)

Source grep on `python/ferrum/models.py`:

```
$ grep -nE "token_bytes|os\.environ|getenv|key\s*=\s*b['\"]" python/ferrum/models.py
```

Only hit: `secrets.token_bytes(_ENC_NONCE_SIZE)` (nonce generation). No
`os.environ`, no `getenv`, no literal key bytes. All encryption keys are obtained
via `self._key_provider.get_key(self.codec_meta.key_id or "")` at query time.

`CodecMeta` fields: `kind`, `pii`, `key_id`, `model_class_name`, `domain_name`,
`element_type` — no key-material field. `KeyProvider` protocol returns `bytes` but
the codec stores only the provider reference, not the key itself.

### PII redaction (C2)

Every codec implements `redact()`:

| Codec | `redact()` output |
|-------|-------------------|
| `PassthroughCodec` (pii=True) | `[REDACTED:pii:passthrough]` |
| `PassthroughCodec` (pii=False) | `[passthrough:<kind>]` |
| `NestedModelCodec` (pii=True) | `[REDACTED:pii:nested_model:<ClassName>]` |
| `NestedListModelCodec` (pii=True) | `[REDACTED:pii:nested_list:<ClassName>]` |
| `EncryptedStringCodec` | `[REDACTED:encrypted_string:key_id=<id>]` |
| `EncryptedJSONCodec` | `[REDACTED:encrypted_json:key_id=<id>]` |
| `InetCodec` (pii=True) | `[REDACTED:pii:inet]` |
| `VectorCodec` | `[vector:dims=<n>]` |

No `redact()` method returns the raw value, ciphertext bytes, or key material.
Encrypted codec redaction includes only the key *identifier*, never the key.

`to_metadata_dict()` (`models.py:351-396`) structurally excludes `codec_meta`,
`codec_pii`, `codec_key_id`, `codec_domain`, `domain_name`, `codec_model` from the
Rust boundary payload (explicit key enumeration, not field iteration). Confirmed
by direct source inspection.

`FerrumCodecError` messages (`models.py:506-525`):
- "Encryption key must be 32 bytes, got N." — size only, no key
- "Ciphertext authentication failed: data was tampered or key is incorrect." — no plaintext
- "Ciphertext too short: expected >= N bytes, got M." — lengths only
- "EncryptedStringCodec expected str, got <type>." — type name only, no value

No error message includes the raw value, ciphertext, key material, or DSN.

### Malformed ciphertext fails safely (C3)

`_decrypt` (`models.py:641-667`):

1. Key size check: `len(key) != _ENC_KEY_SIZE` → `FerrumCodecError` (before any crypto)
2. Length check: `len(data) < _ENC_NONCE_SIZE + _ENC_MAC_SIZE` → `FerrumCodecError`
3. MAC verification: `hmac.compare_digest(mac, expected_mac)` → `FerrumCodecError` if mismatch
4. Decryption only proceeds after MAC verification passes (encrypt-then-MAC order)

MAC is verified **before** decryption — no plaintext is produced from tampered
ciphertext. The error message is generic ("data was tampered or key is incorrect")
to avoid an oracle distinguishing wrong-key from tampered-data.

### Key rotation (C4)

Integration test `test_encrypted_string_key_rotation` (verified in executor log
and independent verification): encrypt with key A → store → fetch → decrypt with A
→ re-encrypt with B → store → fetch → decrypt with B. The codec resolves the key
from the provider by `codec_meta.key_id` at query time. Changing the provider
changes the encryption key without model redefinition.

### Authenticated encryption construction (C5)

`_encrypt` (`models.py:620-636`):
1. Key size enforcement: 32 bytes required
2. Random nonce: `secrets.token_bytes(16)` (NIST-recommended minimum)
3. Key derivation: `HMAC-SHA256(master_key, b"enc:" + nonce)` and
   `HMAC-SHA256(master_key, b"mac:" + nonce)` — domain-separated enc/MAC keys
4. Keystream: SHA-256 counter mode (`sha256(enc_key + struct.pack(">I", counter))`)
5. Ciphertext: `XOR(plaintext, keystream)`
6. MAC: `HMAC-SHA256(mac_key, nonce + ciphertext)` — covers nonce + ciphertext
7. Output: `nonce || mac || ciphertext`

`_decrypt` (`models.py:641-667`):
1. Key size enforcement
2. Minimum length check
3. MAC verification via `hmac.compare_digest` (constant-time)
4. Decryption only after MAC passes

This is a correct encrypt-then-MAC construction. The MAC covers the nonce (prevents
nonce substitution attacks). Constant-time comparison prevents timing oracles.
Domain-separated key derivation prevents cross-use of enc/MAC keys.

### Security-focused tests

```
$ uv run pytest tests/python/unit/test_field_codecs.py -x -q -k "encrypt or redact or pii or key or malform"
136 passed, 41 deselected in 0.30s
```

Covers: encrypted round-trips, non-determinism (nonce randomness), key rotation,
wrong key rejection, missing key, malformed-too-short, malformed-empty, tampered
MAC, tampered ciphertext, invalid input types, PII redaction across all codec
kinds, key-size enforcement, 50× randomized string round-trips, 50× randomized
JSON round-trips.

### Independent adversarial checks (from verification record)

- Random garbage ciphertext (64 bytes) → `FerrumCodecError`, no crash ✓
- Tampered ciphertext (last byte flipped) → `FerrumCodecError`, no plaintext leak ✓
- `redact(plaintext)` → `[REDACTED:encrypted_string:key_id=k]`, no plaintext ✓
- Empty ciphertext → `FerrumCodecError` ✓
- Wrong-size key from provider → `FerrumCodecError` ✓
- PII redaction across all 7 codec kinds: no raw value in any redacted string ✓

## Findings

| # | Severity | Finding | Required correction |
|---|----------|---------|---------------------|
| 1 | Info | The cipher is stdlib-only (SHA-256 keystream + HMAC), not AES-GCM. The executor log documents this as a follow-up risk for FIPS deployments. The `KeyProvider` protocol and `FieldCodec` contract are cipher-agnostic, so a `cryptography`-backed codec can replace this without API changes. | Acceptable for alpha. Document in deployment guide that FIPS environments need a `cryptography`-backed codec. Non-blocking. |
| 2 | Info | `_xor_bytes` uses `zip(data, key, strict=False)`. Currently safe (keystream is always exactly `len(plaintext)`), but `strict=False` would silently truncate if `_keystream` ever produced a shorter output. | Recommend changing to `strict=True` as a defense-in-depth measure. Non-blocking — not currently exploitable. |

No security blockers found. The key-provider injection contract is sound, PII
redaction is comprehensive, malformed ciphertext fails safely with no plaintext
leak, and the encrypt-then-MAC construction is cryptographically correct.

## Decision

`approved`

Encrypted codecs use key-provider injection with no hardcoded keys. PII redaction
excludes codec values from logs/hooks/errors. Malformed ciphertext fails safely
with `FerrumCodecError` and no plaintext leak. Key rotation works via provider
swap. The encrypt-then-MAC construction is cryptographically sound with
constant-time MAC comparison and domain-separated key derivation. The two info
findings are follow-up improvements, not security blockers.
