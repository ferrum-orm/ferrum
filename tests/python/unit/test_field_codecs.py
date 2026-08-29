"""Unit tests for the FieldCodec contract, codec implementations, and model integration.

Covers (W2-A acceptance criteria):
- Typed FieldCodec contract: encode_bind / decode_result / redact.
- CodecMeta immutability and IDE visibility.
- Nested Pydantic model/list JSONB codec round-trips.
- Encrypted string/JSON codecs with key-provider injection.
- Key rotation, key failure, malformed ciphertext, tampered MAC.
- PII redaction (redact never returns raw value).
- citext, inet, bytea, vector, enum, domain, array codecs.
- Field() factory codec parameters and FieldMeta integration.
- Codec registry factory registration and resolution.
- Randomized round-trip tests for encrypted codecs.
"""

from __future__ import annotations

import dataclasses
import ipaddress
import json
import secrets

import pytest

import ferrum
from ferrum.errors import FerrumError
from ferrum.models import (
    CodecMeta,
    EncryptedJSONCodec,
    EncryptedStringCodec,
    FerrumCodecError,
    InetCodec,
    NestedListModelCodec,
    NestedModelCodec,
    PassthroughCodec,
    VectorCodec,
    _decrypt,
    _encrypt,
)
from ferrum.registry import all_codec_kinds, get_codec_factory

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _StaticKeyProvider:
    """Deterministic key provider for tests — keys are fixed at construction."""

    def __init__(self, keys: dict[str, bytes]) -> None:
        for k in keys.values():
            assert len(k) == 32, f"Test key must be 32 bytes, got {len(k)}"
        self._keys = dict(keys)

    def get_key(self, key_id: str) -> bytes:
        if key_id not in self._keys:
            raise FerrumCodecError(
                f"Key {key_id!r} not available.",
                codec_kind="encrypted",
                key_id=key_id,
            )
        return self._keys[key_id]

    def key_ids(self) -> tuple[str, ...]:
        return tuple(self._keys.keys())


def _random_key() -> bytes:
    return secrets.token_bytes(32)


# ---------------------------------------------------------------------------
# CodecMeta immutability and structure
# ---------------------------------------------------------------------------


class TestCodecMeta:
    def test_codec_meta_is_frozen(self) -> None:
        meta = CodecMeta(kind="passthrough", pii=False)
        with pytest.raises(dataclasses.FrozenInstanceError):
            meta.kind = "encrypted"  # type: ignore[misc]

    def test_codec_meta_defaults(self) -> None:
        meta = CodecMeta(kind="citext")
        assert meta.pii is False
        assert meta.key_id is None
        assert meta.model_class_name is None
        assert meta.domain_name is None
        assert meta.element_type is None

    def test_codec_meta_full(self) -> None:
        meta = CodecMeta(
            kind="encrypted_string",
            pii=True,
            key_id="ssn-key",
            model_class_name=None,
            domain_name=None,
            element_type=None,
        )
        assert meta.kind == "encrypted_string"
        assert meta.pii is True
        assert meta.key_id == "ssn-key"

    def test_codec_meta_is_dataclass(self) -> None:
        assert dataclasses.is_dataclass(CodecMeta)


# ---------------------------------------------------------------------------
# FieldCodec protocol compliance
# ---------------------------------------------------------------------------


class TestFieldCodecProtocol:
    def test_passthrough_satisfies_protocol(self) -> None:
        codec = PassthroughCodec(CodecMeta(kind="passthrough"))
        assert hasattr(codec, "codec_meta")
        assert hasattr(codec, "encode_bind")
        assert hasattr(codec, "decode_result")
        assert hasattr(codec, "redact")

    def test_encrypted_string_satisfies_protocol(self) -> None:
        provider = _StaticKeyProvider({"k": _random_key()})
        codec = EncryptedStringCodec(
            CodecMeta(kind="encrypted_string", key_id="k"),
            key_provider=provider,
        )
        assert hasattr(codec, "codec_meta")
        assert hasattr(codec, "encode_bind")
        assert hasattr(codec, "decode_result")
        assert hasattr(codec, "redact")


# ---------------------------------------------------------------------------
# PassthroughCodec
# ---------------------------------------------------------------------------


class TestPassthroughCodec:
    def test_round_trip_str(self) -> None:
        codec = PassthroughCodec(CodecMeta(kind="passthrough"))
        assert codec.encode_bind("hello") == "hello"
        assert codec.decode_result("hello") == "hello"

    def test_round_trip_bytes(self) -> None:
        codec = PassthroughCodec(CodecMeta(kind="bytea"))
        val = b"\x00\x01\x02"
        assert codec.encode_bind(val) is val
        assert codec.decode_result(val) is val

    def test_round_trip_none(self) -> None:
        codec = PassthroughCodec(CodecMeta(kind="passthrough"))
        assert codec.encode_bind(None) is None
        assert codec.decode_result(None) is None

    def test_redact_non_pii(self) -> None:
        codec = PassthroughCodec(CodecMeta(kind="passthrough", pii=False))
        assert "passthrough" in codec.redact("anything")

    def test_redact_pii(self) -> None:
        codec = PassthroughCodec(CodecMeta(kind="passthrough", pii=True))
        result = codec.redact("secret-value")
        assert "REDACTED" in result
        assert "secret-value" not in result


# ---------------------------------------------------------------------------
# NestedModelCodec
# ---------------------------------------------------------------------------


class _Address(ferrum.Model):
    model_config = ferrum.ModelConfig(table="_test_addresses")

    id: int
    street: str
    city: str


class TestNestedModelCodec:
    def test_round_trip_model_instance(self) -> None:
        meta = CodecMeta(kind="nested_model", model_class_name="_Address")
        codec = NestedModelCodec(meta, model_cls=_Address)
        addr = _Address(id=1, street="123 Main", city="Springfield")
        encoded = codec.encode_bind(addr)
        assert isinstance(encoded, dict)
        assert encoded["street"] == "123 Main"
        decoded = codec.decode_result(encoded)
        assert isinstance(decoded, _Address)
        assert decoded.street == "123 Main"
        assert decoded.city == "Springfield"

    def test_round_trip_dict_input(self) -> None:
        meta = CodecMeta(kind="nested_model", model_class_name="_Address")
        codec = NestedModelCodec(meta, model_cls=_Address)
        encoded = codec.encode_bind({"id": 1, "street": "X", "city": "Y"})
        assert encoded == {"id": 1, "street": "X", "city": "Y"}
        decoded = codec.decode_result({"id": 2, "street": "A", "city": "B"})
        assert isinstance(decoded, _Address)
        assert decoded.id == 2

    def test_none_passthrough(self) -> None:
        meta = CodecMeta(kind="nested_model")
        codec = NestedModelCodec(meta, model_cls=_Address)
        assert codec.encode_bind(None) is None
        assert codec.decode_result(None) is None

    def test_json_string_decode(self) -> None:
        meta = CodecMeta(kind="nested_model")
        codec = NestedModelCodec(meta, model_cls=_Address)
        json_str = json.dumps({"id": 1, "street": "S", "city": "C"})
        decoded = codec.decode_result(json_str)
        assert isinstance(decoded, _Address)
        assert decoded.street == "S"

    def test_invalid_input_raises(self) -> None:
        meta = CodecMeta(kind="nested_model")
        codec = NestedModelCodec(meta, model_cls=_Address)
        with pytest.raises(FerrumCodecError):
            codec.encode_bind(123)  # type: ignore[arg-type]

    def test_redact(self) -> None:
        meta = CodecMeta(kind="nested_model", pii=True)
        codec = NestedModelCodec(meta, model_cls=_Address)
        result = codec.redact(_Address(id=1, street="S", city="C"))
        assert "REDACTED" in result
        assert "S" not in result


# ---------------------------------------------------------------------------
# NestedListModelCodec
# ---------------------------------------------------------------------------


class TestNestedListModelCodec:
    def test_round_trip_list_of_models(self) -> None:
        meta = CodecMeta(kind="nested_list", model_class_name="_Address")
        codec = NestedListModelCodec(meta, model_cls=_Address)
        addrs = [
            _Address(id=1, street="A", city="X"),
            _Address(id=2, street="B", city="Y"),
        ]
        encoded = codec.encode_bind(addrs)
        assert isinstance(encoded, list)
        assert len(encoded) == 2
        assert encoded[0]["street"] == "A"
        decoded = codec.decode_result(encoded)
        assert len(decoded) == 2
        assert isinstance(decoded[0], _Address)
        assert decoded[1].city == "Y"

    def test_none_passthrough(self) -> None:
        meta = CodecMeta(kind="nested_list")
        codec = NestedListModelCodec(meta, model_cls=_Address)
        assert codec.encode_bind(None) is None
        assert codec.decode_result(None) is None

    def test_invalid_input_raises(self) -> None:
        meta = CodecMeta(kind="nested_list")
        codec = NestedListModelCodec(meta, model_cls=_Address)
        with pytest.raises(FerrumCodecError):
            codec.encode_bind("not a list")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# EncryptedStringCodec
# ---------------------------------------------------------------------------


class TestEncryptedStringCodec:
    def test_round_trip(self) -> None:
        provider = _StaticKeyProvider({"k": _random_key()})
        meta = CodecMeta(kind="encrypted_string", key_id="k")
        codec = EncryptedStringCodec(meta, key_provider=provider)
        original = "Hello, World!"
        encrypted = codec.encode_bind(original)
        assert isinstance(encrypted, bytes)
        assert encrypted != original.encode("utf-8")
        decrypted = codec.decode_result(encrypted)
        assert decrypted == original

    def test_none_passthrough(self) -> None:
        provider = _StaticKeyProvider({"k": _random_key()})
        meta = CodecMeta(kind="encrypted_string", key_id="k")
        codec = EncryptedStringCodec(meta, key_provider=provider)
        assert codec.encode_bind(None) is None
        assert codec.decode_result(None) is None

    def test_non_deterministic(self) -> None:
        """Same plaintext produces different ciphertext (random nonce)."""
        provider = _StaticKeyProvider({"k": _random_key()})
        meta = CodecMeta(kind="encrypted_string", key_id="k")
        codec = EncryptedStringCodec(meta, key_provider=provider)
        val = "same value"
        c1 = codec.encode_bind(val)
        c2 = codec.encode_bind(val)
        assert c1 != c2
        assert codec.decode_result(c1) == val
        assert codec.decode_result(c2) == val

    def test_unicode_round_trip(self) -> None:
        provider = _StaticKeyProvider({"k": _random_key()})
        meta = CodecMeta(kind="encrypted_string", key_id="k")
        codec = EncryptedStringCodec(meta, key_provider=provider)
        for val in ["", "a", "héllo", "日本語", "🎉", "x" * 1000]:
            encrypted = codec.encode_bind(val)
            decrypted = codec.decode_result(encrypted)
            assert decrypted == val

    def test_key_rotation_success(self) -> None:
        """Decrypt with old key, re-encrypt with new key."""
        key_old = _random_key()
        key_new = _random_key()
        provider_old = _StaticKeyProvider({"k": key_old})
        provider_new = _StaticKeyProvider({"k": key_new})
        meta = CodecMeta(kind="encrypted_string", key_id="k")
        codec_old = EncryptedStringCodec(meta, key_provider=provider_old)
        codec_new = EncryptedStringCodec(meta, key_provider=provider_new)

        original = "secret data"
        encrypted_old = codec_old.encode_bind(original)
        # Decrypt with old key (still works)
        assert codec_old.decode_result(encrypted_old) == original
        # Re-encrypt with new key
        encrypted_new = codec_new.encode_bind(original)
        # Decrypt with new key
        assert codec_new.decode_result(encrypted_new) == original
        # Old key cannot decrypt new ciphertext
        with pytest.raises(FerrumCodecError):
            codec_old.decode_result(encrypted_new)

    def test_wrong_key_rejected(self) -> None:
        key_a = _random_key()
        key_b = _random_key()
        provider_a = _StaticKeyProvider({"k": key_a})
        provider_b = _StaticKeyProvider({"k": key_b})
        meta = CodecMeta(kind="encrypted_string", key_id="k")
        codec_a = EncryptedStringCodec(meta, key_provider=provider_a)
        codec_b = EncryptedStringCodec(meta, key_provider=provider_b)

        encrypted = codec_a.encode_bind("secret")
        with pytest.raises(FerrumCodecError, match="authentication failed"):
            codec_b.decode_result(encrypted)

    def test_missing_key_raises(self) -> None:
        provider = _StaticKeyProvider({"other": _random_key()})
        meta = CodecMeta(kind="encrypted_string", key_id="missing")
        codec = EncryptedStringCodec(meta, key_provider=provider)
        with pytest.raises(FerrumCodecError):
            codec.encode_bind("test")

    def test_malformed_ciphertext_too_short(self) -> None:
        provider = _StaticKeyProvider({"k": _random_key()})
        meta = CodecMeta(kind="encrypted_string", key_id="k")
        codec = EncryptedStringCodec(meta, key_provider=provider)
        with pytest.raises(FerrumCodecError, match="too short"):
            codec.decode_result(b"short")

    def test_malformed_ciphertext_empty(self) -> None:
        provider = _StaticKeyProvider({"k": _random_key()})
        meta = CodecMeta(kind="encrypted_string", key_id="k")
        codec = EncryptedStringCodec(meta, key_provider=provider)
        with pytest.raises(FerrumCodecError, match="too short"):
            codec.decode_result(b"")

    def test_tampered_mac_rejected(self) -> None:
        provider = _StaticKeyProvider({"k": _random_key()})
        meta = CodecMeta(kind="encrypted_string", key_id="k")
        codec = EncryptedStringCodec(meta, key_provider=provider)
        encrypted = bytearray(codec.encode_bind("secret"))
        # Tamper with the MAC (bytes 16..48)
        encrypted[20] ^= 0x01
        with pytest.raises(FerrumCodecError, match="authentication failed"):
            codec.decode_result(bytes(encrypted))

    def test_tampered_ciphertext_rejected(self) -> None:
        provider = _StaticKeyProvider({"k": _random_key()})
        meta = CodecMeta(kind="encrypted_string", key_id="k")
        codec = EncryptedStringCodec(meta, key_provider=provider)
        encrypted = bytearray(codec.encode_bind("secret"))
        # Tamper with the ciphertext (after nonce + MAC)
        encrypted[-1] ^= 0x01
        with pytest.raises(FerrumCodecError, match="authentication failed"):
            codec.decode_result(bytes(encrypted))

    def test_invalid_input_type_raises(self) -> None:
        provider = _StaticKeyProvider({"k": _random_key()})
        meta = CodecMeta(kind="encrypted_string", key_id="k")
        codec = EncryptedStringCodec(meta, key_provider=provider)
        with pytest.raises(FerrumCodecError):
            codec.encode_bind(123)  # type: ignore[arg-type]

    def test_pii_redaction(self) -> None:
        provider = _StaticKeyProvider({"k": _random_key()})
        meta = CodecMeta(kind="encrypted_string", key_id="k", pii=True)
        codec = EncryptedStringCodec(meta, key_provider=provider)
        result = codec.redact("very-secret-value")
        assert "REDACTED" in result
        assert "very-secret-value" not in result
        assert "k" in result  # key_id is safe to show

    def test_requires_key_id(self) -> None:
        provider = _StaticKeyProvider({"k": _random_key()})
        meta = CodecMeta(kind="encrypted_string", key_id=None)
        with pytest.raises(FerrumCodecError):
            EncryptedStringCodec(meta, key_provider=provider)


# ---------------------------------------------------------------------------
# EncryptedJSONCodec
# ---------------------------------------------------------------------------


class TestEncryptedJSONCodec:
    def test_round_trip_dict(self) -> None:
        provider = _StaticKeyProvider({"k": _random_key()})
        meta = CodecMeta(kind="encrypted_json", key_id="k")
        codec = EncryptedJSONCodec(meta, key_provider=provider)
        data = {"name": "Alice", "age": 30, "nested": {"x": [1, 2]}}
        encrypted = codec.encode_bind(data)
        assert isinstance(encrypted, bytes)
        decrypted = codec.decode_result(encrypted)
        assert decrypted == data

    def test_round_trip_list(self) -> None:
        provider = _StaticKeyProvider({"k": _random_key()})
        meta = CodecMeta(kind="encrypted_json", key_id="k")
        codec = EncryptedJSONCodec(meta, key_provider=provider)
        data = [1, "two", {"three": 3}, [4, 5]]
        encrypted = codec.encode_bind(data)
        decrypted = codec.decode_result(encrypted)
        assert decrypted == data

    def test_none_passthrough(self) -> None:
        provider = _StaticKeyProvider({"k": _random_key()})
        meta = CodecMeta(kind="encrypted_json", key_id="k")
        codec = EncryptedJSONCodec(meta, key_provider=provider)
        assert codec.encode_bind(None) is None
        assert codec.decode_result(None) is None

    def test_wrong_key_rejected(self) -> None:
        key_a = _random_key()
        key_b = _random_key()
        codec_a = EncryptedJSONCodec(
            CodecMeta(kind="encrypted_json", key_id="k"),
            key_provider=_StaticKeyProvider({"k": key_a}),
        )
        codec_b = EncryptedJSONCodec(
            CodecMeta(kind="encrypted_json", key_id="k"),
            key_provider=_StaticKeyProvider({"k": key_b}),
        )
        encrypted = codec_a.encode_bind({"x": 1})
        with pytest.raises(FerrumCodecError):
            codec_b.decode_result(encrypted)

    def test_pii_redaction(self) -> None:
        provider = _StaticKeyProvider({"k": _random_key()})
        meta = CodecMeta(kind="encrypted_json", key_id="k", pii=True)
        codec = EncryptedJSONCodec(meta, key_provider=provider)
        result = codec.redact({"secret": "data"})
        assert "REDACTED" in result
        assert "secret" not in result


# ---------------------------------------------------------------------------
# InetCodec
# ---------------------------------------------------------------------------


class TestInetCodec:
    def test_round_trip_str_ipv4(self) -> None:
        codec = InetCodec(CodecMeta(kind="inet"))
        assert codec.encode_bind("192.168.1.1") == "192.168.1.1"
        result = codec.decode_result("10.0.0.1")
        assert isinstance(result, ipaddress.IPv4Address)

    def test_round_trip_ipaddress(self) -> None:
        codec = InetCodec(CodecMeta(kind="inet"))
        addr = ipaddress.IPv4Address("172.16.0.1")
        assert codec.encode_bind(addr) == "172.16.0.1"
        decoded = codec.decode_result("172.16.0.1")
        assert decoded == addr

    def test_round_trip_ipv6(self) -> None:
        codec = InetCodec(CodecMeta(kind="inet"))
        result = codec.decode_result("::1")
        assert isinstance(result, ipaddress.IPv6Address)
        assert str(result) == "::1"

    def test_none_passthrough(self) -> None:
        codec = InetCodec(CodecMeta(kind="inet"))
        assert codec.encode_bind(None) is None
        assert codec.decode_result(None) is None

    def test_invalid_input_raises(self) -> None:
        codec = InetCodec(CodecMeta(kind="inet"))
        with pytest.raises(FerrumCodecError):
            codec.encode_bind(123)  # type: ignore[arg-type]

    def test_redact_non_pii(self) -> None:
        codec = InetCodec(CodecMeta(kind="inet", pii=False))
        assert "inet" in codec.redact("192.168.1.1")

    def test_redact_pii(self) -> None:
        codec = InetCodec(CodecMeta(kind="inet", pii=True))
        result = codec.redact("192.168.1.1")
        assert "REDACTED" in result
        assert "192.168.1.1" not in result


# ---------------------------------------------------------------------------
# VectorCodec
# ---------------------------------------------------------------------------


class TestVectorCodec:
    def test_round_trip_list(self) -> None:
        codec = VectorCodec(CodecMeta(kind="vector"), dimensions=3)
        encoded = codec.encode_bind([1.0, 2.0, 3.0])
        assert encoded == "[1.0,2.0,3.0]"
        decoded = codec.decode_result("[1.0,2.0,3.0]")
        assert decoded == [1.0, 2.0, 3.0]

    def test_none_passthrough(self) -> None:
        codec = VectorCodec(CodecMeta(kind="vector"), dimensions=3)
        assert codec.encode_bind(None) is None
        assert codec.decode_result(None) is None

    def test_string_passthrough(self) -> None:
        codec = VectorCodec(CodecMeta(kind="vector"), dimensions=3)
        assert codec.encode_bind("[1,2,3]") == "[1,2,3]"

    def test_dimension_validation(self) -> None:
        codec = VectorCodec(CodecMeta(kind="vector"), dimensions=3)
        with pytest.raises(FerrumCodecError, match="3 dimensions"):
            codec.encode_bind([1.0, 2.0])

    def test_no_dimensions_no_validation(self) -> None:
        codec = VectorCodec(CodecMeta(kind="vector"), dimensions=None)
        assert codec.encode_bind([1.0]) == "[1.0]"
        assert codec.encode_bind([1.0, 2.0]) == "[1.0,2.0]"

    def test_empty_vector_decode(self) -> None:
        codec = VectorCodec(CodecMeta(kind="vector"))
        assert codec.decode_result("[]") == []

    def test_invalid_input_raises(self) -> None:
        codec = VectorCodec(CodecMeta(kind="vector"))
        with pytest.raises(FerrumCodecError):
            codec.encode_bind(123)  # type: ignore[arg-type]

    def test_redact(self) -> None:
        codec = VectorCodec(CodecMeta(kind="vector"), dimensions=128)
        result = codec.redact([1.0] * 128)
        assert "vector" in result
        assert "128" in result


# ---------------------------------------------------------------------------
# Encryption primitives (low-level)
# ---------------------------------------------------------------------------


class TestEncryptionPrimitives:
    def test_encrypt_decrypt_round_trip(self) -> None:
        key = _random_key()
        plaintext = b"test plaintext data"
        ciphertext = _encrypt(plaintext, key)
        assert ciphertext != plaintext
        assert _decrypt(ciphertext, key) == plaintext

    def test_encrypt_is_non_deterministic(self) -> None:
        key = _random_key()
        plaintext = b"same"
        c1 = _encrypt(plaintext, key)
        c2 = _encrypt(plaintext, key)
        assert c1 != c2

    def test_decrypt_wrong_key_raises(self) -> None:
        key_a = _random_key()
        key_b = _random_key()
        ciphertext = _encrypt(b"secret", key_a)
        with pytest.raises(FerrumCodecError, match="authentication failed"):
            _decrypt(ciphertext, key_b)

    def test_decrypt_short_data_raises(self) -> None:
        key = _random_key()
        with pytest.raises(FerrumCodecError, match="too short"):
            _decrypt(b"short", key)

    def test_decrypt_tampered_raises(self) -> None:
        key = _random_key()
        ciphertext = bytearray(_encrypt(b"secret", key))
        ciphertext[-1] ^= 0xFF
        with pytest.raises(FerrumCodecError, match="authentication failed"):
            _decrypt(bytes(ciphertext), key)

    def test_wrong_key_size_raises(self) -> None:
        with pytest.raises(FerrumCodecError, match="32 bytes"):
            _encrypt(b"test", b"short")

    def test_empty_plaintext_round_trip(self) -> None:
        key = _random_key()
        ciphertext = _encrypt(b"", key)
        assert _decrypt(ciphertext, key) == b""


# ---------------------------------------------------------------------------
# Randomized round-trip tests
# ---------------------------------------------------------------------------


class TestRandomizedRoundTrips:
    @pytest.mark.parametrize("iteration", range(50))
    def test_random_string_encryption_round_trip(self, iteration: int) -> None:
        provider = _StaticKeyProvider({"k": _random_key()})
        codec = EncryptedStringCodec(
            CodecMeta(kind="encrypted_string", key_id="k"),
            key_provider=provider,
        )
        length = secrets.randbelow(256)
        val = secrets.token_urlsafe(length)
        encrypted = codec.encode_bind(val)
        decrypted = codec.decode_result(encrypted)
        assert decrypted == val

    @pytest.mark.parametrize("iteration", range(50))
    def test_random_json_encryption_round_trip(self, iteration: int) -> None:
        provider = _StaticKeyProvider({"k": _random_key()})
        codec = EncryptedJSONCodec(
            CodecMeta(kind="encrypted_json", key_id="k"),
            key_provider=provider,
        )
        data = {
            "id": secrets.randbelow(10000),
            "name": secrets.token_urlsafe(10),
            "tags": [secrets.token_urlsafe(4) for _ in range(secrets.randbelow(5))],
            "nested": {"x": secrets.randbelow(100)},
        }
        encrypted = codec.encode_bind(data)
        decrypted = codec.decode_result(encrypted)
        assert decrypted == data


# ---------------------------------------------------------------------------
# Field() factory integration with codecs
# ---------------------------------------------------------------------------


class _CodecUser(ferrum.Model):
    model_config = ferrum.ModelConfig(table="_codec_users")

    id: int
    email: str = ferrum.Field(codec_kind="citext")
    ssn: str = ferrum.Field(
        codec_kind="encrypted_string",
        codec_key_id="ssn-key",
        codec_pii=True,
        nullable=True,
    )
    ip_addr: str = ferrum.Field(codec_kind="inet", nullable=True)


class _CodecProduct(ferrum.Model):
    model_config = ferrum.ModelConfig(table="_codec_products")

    id: int
    price: float = ferrum.Field(codec_kind="domain", codec_domain="positive_price")


class _CodecProfile(ferrum.Model):
    model_config = ferrum.ModelConfig(table="_codec_profiles")

    id: int
    address: dict = ferrum.Field(
        codec_kind="nested_model",
        codec_model="_Address",
        nullable=True,
    )


class TestFieldFactoryCodecIntegration:
    def test_citext_field_type_override(self) -> None:
        meta = _CodecUser.get_metadata()
        email = next(f for f in meta.fields if f.name == "email")
        assert email.field_type == "citext"
        assert email.sql_type == "CITEXT"
        assert email.codec_meta is not None
        assert email.codec_meta.kind == "citext"

    def test_encrypted_string_field_type_override(self) -> None:
        meta = _CodecUser.get_metadata()
        ssn = next(f for f in meta.fields if f.name == "ssn")
        assert ssn.field_type == "bytes"
        assert ssn.sql_type == "BYTEA"
        assert ssn.codec_meta is not None
        assert ssn.codec_meta.kind == "encrypted_string"
        assert ssn.codec_meta.pii is True
        assert ssn.codec_meta.key_id == "ssn-key"

    def test_inet_field_type_override(self) -> None:
        meta = _CodecUser.get_metadata()
        ip = next(f for f in meta.fields if f.name == "ip_addr")
        assert ip.field_type == "inet"
        assert ip.sql_type == "INET"
        assert ip.codec_meta is not None
        assert ip.codec_meta.kind == "inet"

    def test_domain_field_type_override(self) -> None:
        meta = _CodecProduct.get_metadata()
        price = next(f for f in meta.fields if f.name == "price")
        assert price.field_type == "domain"
        assert price.sql_type == "positive_price"
        assert price.codec_meta is not None
        assert price.codec_meta.domain_name == "positive_price"

    def test_nested_model_field_type_override(self) -> None:
        meta = _CodecProfile.get_metadata()
        addr = next(f for f in meta.fields if f.name == "address")
        assert addr.field_type == "json"
        assert addr.sql_type == "JSONB"
        assert addr.codec_meta is not None
        assert addr.codec_meta.kind == "nested_model"
        assert addr.codec_meta.model_class_name == "_Address"

    def test_codec_meta_is_in_field_meta(self) -> None:
        meta = _CodecUser.get_metadata()
        for field in meta.fields:
            if field.name in ("email", "ssn", "ip_addr"):
                assert field.codec_meta is not None
            else:
                assert field.codec_meta is None

    def test_field_without_codec_has_no_codec_meta(self) -> None:
        meta = _CodecUser.get_metadata()
        id_field = next(f for f in meta.fields if f.name == "id")
        assert id_field.codec_meta is None
        assert id_field.domain_name is None


# ---------------------------------------------------------------------------
# Field() factory validation
# ---------------------------------------------------------------------------


class TestFieldFactoryValidation:
    def test_encrypted_string_requires_key_id(self) -> None:
        with pytest.raises(ValueError, match="codec_key_id"):

            class _BadModel(ferrum.Model):
                model_config = ferrum.ModelConfig(table="_bad_encrypted")

                id: int
                ssn: str = ferrum.Field(codec_kind="encrypted_string")

    def test_domain_requires_domain_name(self) -> None:
        with pytest.raises(ValueError, match="codec_domain"):

            class _BadDomain(ferrum.Model):
                model_config = ferrum.ModelConfig(table="_bad_domain")

                id: int
                price: float = ferrum.Field(codec_kind="domain")


# ---------------------------------------------------------------------------
# Codec registry
# ---------------------------------------------------------------------------


class TestCodecRegistry:
    def test_default_codecs_registered(self) -> None:
        kinds = all_codec_kinds()
        for expected in (
            "passthrough",
            "bytea",
            "citext",
            "enum",
            "domain",
            "array",
            "encrypted_string",
            "encrypted_json",
            "nested_model",
            "nested_list",
            "inet",
            "vector",
        ):
            assert expected in kinds, f"Missing codec kind: {expected}"

    def test_get_codec_factory_returns_callable(self) -> None:
        factory = get_codec_factory("passthrough")
        assert callable(factory)
        codec = factory(CodecMeta(kind="passthrough"))
        assert isinstance(codec, PassthroughCodec)

    def test_get_unknown_codec_factory_raises(self) -> None:
        with pytest.raises(FerrumError):
            get_codec_factory("nonexistent")

    def test_encrypted_string_factory_requires_key_provider(self) -> None:
        factory = get_codec_factory("encrypted_string")
        with pytest.raises(FerrumCodecError, match="key_provider"):
            factory(CodecMeta(kind="encrypted_string", key_id="k"))

    def test_nested_model_factory_resolves_model(self) -> None:
        factory = get_codec_factory("nested_model")
        codec = factory(CodecMeta(kind="nested_model", model_class_name="_Address"))
        assert isinstance(codec, NestedModelCodec)


# ---------------------------------------------------------------------------
# Metadata boundary exclusion (codec metadata NOT in Rust payload)
# ---------------------------------------------------------------------------


class TestCodecMetadataExclusion:
    def test_codec_meta_not_in_metadata_dict(self) -> None:
        """CodecMeta must not leak into the Rust boundary payload."""
        meta = _CodecUser.get_metadata()
        payload = meta.to_metadata_dict()
        for field_payload in payload["fields"]:
            # field_type is sent (needed for SQL compilation), but
            # codec_meta, key_id, domain_name, pii must NOT be present.
            assert "codec_meta" not in field_payload
            assert "codec_pii" not in field_payload
            assert "codec_key_id" not in field_payload
            assert "codec_domain" not in field_payload

    def test_field_type_sent_in_metadata_dict(self) -> None:
        """The overridden field_type IS sent (Rust needs it for type casting)."""
        meta = _CodecUser.get_metadata()
        payload = meta.to_metadata_dict()
        email_payload = next(f for f in payload["fields"] if f["name"] == "email")
        assert email_payload["field_type"] == "citext"
