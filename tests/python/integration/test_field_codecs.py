"""Integration tests for field codecs against a live PostgreSQL instance.

These tests create transient tables, insert data through raw asyncpg SQL,
and verify that the codec contract (encode_bind / decode_result) produces
values that PostgreSQL accepts and returns correctly. The tests exercise
the codec layer directly — queryset hydration integration is W2-B.

Requires:
    FERRUM_TEST_DSN → PostgreSQL DSN for the test database
    citext extension (CREATE EXTENSION IF NOT EXISTS citext)
"""

# ruff: noqa: S608 — table identifiers are test-controlled suffixes, not user input.

from __future__ import annotations

import ipaddress
import json
import secrets
from typing import Any

import pytest

import ferrum
from ferrum.models import (
    CodecMeta,
    EncryptedJSONCodec,
    EncryptedStringCodec,
    FerrumCodecError,
    InetCodec,
    NestedListModelCodec,
    NestedModelCodec,
    PassthroughCodec,
)

# ---------------------------------------------------------------------------
# Test key provider
# ---------------------------------------------------------------------------


class _TestKeyProvider:
    def __init__(self, keys: dict[str, bytes]) -> None:
        self._keys = keys

    def get_key(self, key_id: str) -> bytes:
        if key_id not in self._keys:
            raise FerrumCodecError(f"Key {key_id!r} not found", codec_kind="encrypted")
        return self._keys[key_id]

    def key_ids(self) -> tuple[str, ...]:
        return tuple(self._keys.keys())


# ---------------------------------------------------------------------------
# Fixture: transient codec table
# ---------------------------------------------------------------------------


@pytest.fixture
async def codec_table(pg_conn: ferrum.connection.Connection, unique_suffix: str) -> str:
    """Create a transient table with codec-backed columns and return its name."""
    table_name = f"_codec_test_{unique_suffix}"
    driver = pg_conn._require_driver()
    await driver.execute("CREATE EXTENSION IF NOT EXISTS citext")
    await driver.execute(
        f"""
        CREATE TABLE {table_name} (
            id SERIAL PRIMARY KEY,
            email CITEXT,
            ip_addr INET,
            secret BYTEA,
            metadata JSONB,
            tags TEXT[]
        )
        """
    )
    yield table_name
    await driver.execute(f"DROP TABLE IF EXISTS {table_name}")


@pytest.fixture
async def domain_table(pg_conn: ferrum.connection.Connection, unique_suffix: str) -> str:
    """Create a transient domain + table and return the table name."""
    domain_name = f"_pos_price_{unique_suffix}"
    table_name = f"_domain_test_{unique_suffix}"
    driver = pg_conn._require_driver()
    await driver.execute(f"CREATE DOMAIN {domain_name} AS NUMERIC CHECK (VALUE > 0)")
    await driver.execute(
        f"""
        CREATE TABLE {table_name} (
            id SERIAL PRIMARY KEY,
            price {domain_name}
        )
        """
    )
    yield table_name
    await driver.execute(f"DROP TABLE IF EXISTS {table_name}")
    await driver.execute(f"DROP DOMAIN IF EXISTS {domain_name}")


# ---------------------------------------------------------------------------
# CITEXT round-trip
# ---------------------------------------------------------------------------


class TestCitextIntegration:
    @pytest.mark.asyncio
    async def test_citext_case_insensitive(self, codec_table: str, pg_conn: Any) -> None:
        driver = pg_conn._require_driver()
        await driver.execute(f"INSERT INTO {codec_table} (email) VALUES ($1)", "User@Example.COM")
        row = await driver.fetchrow(
            f"SELECT email FROM {codec_table} WHERE email = $1", "user@example.com"
        )
        assert row is not None
        assert row["email"] == "User@Example.COM"

        codec = PassthroughCodec(CodecMeta(kind="citext"))
        assert codec.encode_bind("Test@Test.com") == "Test@Test.com"


# ---------------------------------------------------------------------------
# INET round-trip
# ---------------------------------------------------------------------------


class TestInetIntegration:
    @pytest.mark.asyncio
    async def test_inet_round_trip(self, codec_table: str, pg_conn: Any) -> None:
        driver = pg_conn._require_driver()
        addr = "192.168.1.100"
        await driver.execute(f"INSERT INTO {codec_table} (ip_addr) VALUES ($1)", addr)
        row = await driver.fetchrow(f"SELECT ip_addr FROM {codec_table} WHERE ip_addr = $1", addr)
        assert row is not None
        assert str(row["ip_addr"]) == addr

    @pytest.mark.asyncio
    async def test_inet_codec_round_trip(self, codec_table: str, pg_conn: Any) -> None:
        driver = pg_conn._require_driver()
        codec = InetCodec(CodecMeta(kind="inet"))
        addr = "10.0.0.1"
        encoded = codec.encode_bind(addr)
        await driver.execute(f"INSERT INTO {codec_table} (ip_addr) VALUES ($1)", encoded)
        row = await driver.fetchrow(f"SELECT ip_addr FROM {codec_table}")
        assert row is not None
        decoded = codec.decode_result(str(row["ip_addr"]))
        assert isinstance(decoded, ipaddress.IPv4Address)
        assert str(decoded) == addr

    @pytest.mark.asyncio
    async def test_inet_ipv6(self, codec_table: str, pg_conn: Any) -> None:
        driver = pg_conn._require_driver()
        addr = "2001:db8::1"
        await driver.execute(f"INSERT INTO {codec_table} (ip_addr) VALUES ($1)", addr)
        row = await driver.fetchrow(f"SELECT ip_addr FROM {codec_table} WHERE ip_addr = $1", addr)
        assert row is not None
        codec = InetCodec(CodecMeta(kind="inet"))
        decoded = codec.decode_result(str(row["ip_addr"]))
        assert isinstance(decoded, ipaddress.IPv6Address)


# ---------------------------------------------------------------------------
# BYTEA / Encrypted string round-trip
# ---------------------------------------------------------------------------


class TestByteaIntegration:
    @pytest.mark.asyncio
    async def test_bytea_round_trip(self, codec_table: str, pg_conn: Any) -> None:
        driver = pg_conn._require_driver()
        data = b"\x00\x01\x02\x03\xff\xfe"
        await driver.execute(f"INSERT INTO {codec_table} (secret) VALUES ($1)", data)
        row = await driver.fetchrow(f"SELECT secret FROM {codec_table}")
        assert row is not None
        assert bytes(row["secret"]) == data

    @pytest.mark.asyncio
    async def test_encrypted_string_round_trip(self, codec_table: str, pg_conn: Any) -> None:
        driver = pg_conn._require_driver()
        key = secrets.token_bytes(32)
        provider = _TestKeyProvider({"test-key": key})
        codec = EncryptedStringCodec(
            CodecMeta(kind="encrypted_string", key_id="test-key", pii=True),
            key_provider=provider,
        )
        plaintext = "SuperSecret SSN 123-45-6789"
        encrypted = codec.encode_bind(plaintext)
        assert isinstance(encrypted, bytes)

        await driver.execute(f"INSERT INTO {codec_table} (secret) VALUES ($1)", encrypted)
        row = await driver.fetchrow(f"SELECT secret FROM {codec_table}")
        assert row is not None
        db_value = bytes(row["secret"])
        assert db_value == encrypted
        decrypted = codec.decode_result(db_value)
        assert decrypted == plaintext

    @pytest.mark.asyncio
    async def test_encrypted_string_key_rotation(self, codec_table: str, pg_conn: Any) -> None:
        """Encrypt with key A, store, then decrypt with key A and re-encrypt with B."""
        driver = pg_conn._require_driver()
        key_a = secrets.token_bytes(32)
        key_b = secrets.token_bytes(32)
        provider_a = _TestKeyProvider({"k": key_a})
        provider_b = _TestKeyProvider({"k": key_b})
        codec_a = EncryptedStringCodec(
            CodecMeta(kind="encrypted_string", key_id="k"), key_provider=provider_a
        )
        codec_b = EncryptedStringCodec(
            CodecMeta(kind="encrypted_string", key_id="k"), key_provider=provider_b
        )

        plaintext = "rotating secret"
        encrypted_a = codec_a.encode_bind(plaintext)
        await driver.execute(f"INSERT INTO {codec_table} (secret) VALUES ($1)", encrypted_a)

        row = await driver.fetchrow(f"SELECT secret FROM {codec_table}")
        assert row is not None
        db_value = bytes(row["secret"])
        assert codec_a.decode_result(db_value) == plaintext
        with pytest.raises(FerrumCodecError):
            codec_b.decode_result(db_value)

        # Re-encrypt with new key
        encrypted_b = codec_b.encode_bind(plaintext)
        await driver.execute(f"UPDATE {codec_table} SET secret = $1", encrypted_b)
        row2 = await driver.fetchrow(f"SELECT secret FROM {codec_table}")
        assert row2 is not None
        assert codec_b.decode_result(bytes(row2["secret"])) == plaintext


# ---------------------------------------------------------------------------
# JSONB / Nested model round-trip
# ---------------------------------------------------------------------------


class _NestedAddr(ferrum.Model):
    model_config = ferrum.ModelConfig(table="_nested_addr_integration")

    id: int
    street: str
    city: str


class TestNestedModelIntegration:
    @pytest.mark.asyncio
    async def test_nested_model_round_trip(self, codec_table: str, pg_conn: Any) -> None:
        driver = pg_conn._require_driver()
        codec = NestedModelCodec(
            CodecMeta(kind="nested_model", model_class_name="_NestedAddr"),
            model_cls=_NestedAddr,
        )
        addr = _NestedAddr(id=1, street="123 Main St", city="Springfield")
        encoded = codec.encode_bind(addr)
        assert isinstance(encoded, dict)

        await driver.execute(
            f"INSERT INTO {codec_table} (metadata) VALUES ($1::jsonb)",
            json.dumps(encoded),
        )
        row = await driver.fetchrow(f"SELECT metadata FROM {codec_table}")
        assert row is not None
        db_value = row["metadata"]
        if isinstance(db_value, str):
            db_value = json.loads(db_value)
        decoded = codec.decode_result(db_value)
        assert isinstance(decoded, _NestedAddr)
        assert decoded.street == "123 Main St"
        assert decoded.city == "Springfield"

    @pytest.mark.asyncio
    async def test_nested_list_round_trip(self, codec_table: str, pg_conn: Any) -> None:
        driver = pg_conn._require_driver()
        codec = NestedListModelCodec(
            CodecMeta(kind="nested_list", model_class_name="_NestedAddr"),
            model_cls=_NestedAddr,
        )
        addrs = [
            _NestedAddr(id=1, street="A", city="X"),
            _NestedAddr(id=2, street="B", city="Y"),
        ]
        encoded = codec.encode_bind(addrs)
        assert isinstance(encoded, list)

        await driver.execute(
            f"INSERT INTO {codec_table} (metadata) VALUES ($1::jsonb)",
            json.dumps(encoded),
        )
        row = await driver.fetchrow(f"SELECT metadata FROM {codec_table}")
        assert row is not None
        db_value = row["metadata"]
        if isinstance(db_value, str):
            db_value = json.loads(db_value)
        decoded = codec.decode_result(db_value)
        assert len(decoded) == 2
        assert isinstance(decoded[0], _NestedAddr)
        assert decoded[1].city == "Y"


# ---------------------------------------------------------------------------
# Encrypted JSON round-trip
# ---------------------------------------------------------------------------


class TestEncryptedJSONIntegration:
    @pytest.mark.asyncio
    async def test_encrypted_json_round_trip(self, codec_table: str, pg_conn: Any) -> None:
        driver = pg_conn._require_driver()
        key = secrets.token_bytes(32)
        provider = _TestKeyProvider({"j-key": key})
        codec = EncryptedJSONCodec(
            CodecMeta(kind="encrypted_json", key_id="j-key"), key_provider=provider
        )
        data = {"name": "Alice", "age": 30, "items": [1, 2, 3]}
        encrypted = codec.encode_bind(data)
        assert isinstance(encrypted, bytes)

        await driver.execute(f"INSERT INTO {codec_table} (secret) VALUES ($1)", encrypted)
        row = await driver.fetchrow(f"SELECT secret FROM {codec_table}")
        assert row is not None
        db_value = bytes(row["secret"])
        decrypted = codec.decode_result(db_value)
        assert decrypted == data


# ---------------------------------------------------------------------------
# Custom domain round-trip
# ---------------------------------------------------------------------------


class TestDomainIntegration:
    @pytest.mark.asyncio
    async def test_domain_round_trip(self, domain_table: str, pg_conn: Any) -> None:
        driver = pg_conn._require_driver()
        await driver.execute(f"INSERT INTO {domain_table} (price) VALUES ($1)", 19.99)
        row = await driver.fetchrow(f"SELECT price FROM {domain_table}")
        assert row is not None
        assert float(row["price"]) == 19.99

    @pytest.mark.asyncio
    async def test_domain_constraint_enforced(self, domain_table: str, pg_conn: Any) -> None:
        driver = pg_conn._require_driver()
        with pytest.raises(Exception):  # noqa: B017
            await driver.execute(f"INSERT INTO {domain_table} (price) VALUES ($1)", -1.0)


# ---------------------------------------------------------------------------
# Array round-trip
# ---------------------------------------------------------------------------


class TestArrayIntegration:
    @pytest.mark.asyncio
    async def test_text_array_round_trip(self, codec_table: str, pg_conn: Any) -> None:
        driver = pg_conn._require_driver()
        tags = ["python", "rust", "postgres"]
        await driver.execute(f"INSERT INTO {codec_table} (tags) VALUES ($1)", tags)
        row = await driver.fetchrow(f"SELECT tags FROM {codec_table}")
        assert row is not None
        assert list(row["tags"]) == tags
