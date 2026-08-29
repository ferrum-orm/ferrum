"""Live-PostgreSQL and source-inspection contract tests for Org AI Platform
(Onyx-fork) persistence patterns.

Retargeted (W1-B/W1-F/W2-A implemented): the three stale "missing API"
entries (``schema_transaction``, ``ShardRouter``/``ConnectionRegistry``,
``select_for_update``) now verify the APIs exist and work against a live
PostgreSQL instance, plus validation tests for schema-per-tenant routing
(allowlist + transaction-local ``search_path`` reset), shard routing
(trusted keys + connection-explicit QuerySet), and encrypted/JSON codecs
(key-provider injection + PII redaction).

Manifest coverage: oai-01 (schema-per-tenant), oai-02 (shard routing),
oai-03/oai-04 (SELECT ... FOR UPDATE [SKIP LOCKED|NOWAIT]), oai-06 (nested
Pydantic JSONB field type), oai-07 (conditional-COALESCE upsert),
oai-10 (schema-scoped drift detection).
"""

from __future__ import annotations

import inspect
import json
import secrets
from typing import Annotated

import pydantic
import pytest

import ferrum
from ferrum.errors import FerrumCompileError, FerrumConfigError, FerrumTimeoutError
from ferrum.migrations import apply
from ferrum.migrations import operations as ops
from ferrum.migrations.drift import detect_drift
from ferrum.models import (
    CodecMeta,
    EncryptedJSONCodec,
    EncryptedStringCodec,
    FerrumCodecError,
)
from ferrum.queryset import QuerySet
from ferrum.routing import ConnectionRegistry, PoolConfig, ShardRouter
from ferrum.session import (
    ALLOWED_SCHEMA_NAMES,
    schema_transaction,
)

# ---------------------------------------------------------------------------
# Retargeted oai-01: schema_transaction EXISTS and works
# ---------------------------------------------------------------------------


def test_schema_per_tenant_routing_api_is_available() -> None:
    """Manifest oai-01 (retargeted): ``schema_transaction`` is now part of the
    public Ferrum surface. The ratified W1-F contract (AGENTS.md §5a) exposes
    it as a top-level ``ferrum`` re-export and on the ``ferrum.session``
    module. Schema identifiers are validated against an identifier regex AND
    an allowlist — never interpolated from untrusted input.
    """
    assert hasattr(ferrum, "schema_transaction")
    assert hasattr(ferrum.session, "schema_transaction")
    # The function is callable and takes (conn, schema, *, allowed_schemas,
    # isolation, readonly) — verified by signature inspection, not by
    # invoking it here (it requires a live Connection).
    sig = inspect.signature(ferrum.schema_transaction)
    assert "schema" in sig.parameters
    assert "allowed_schemas" in sig.parameters
    # The schema allowlist defaults to {"public"} — the strict allowlist is
    # the structural safety property (§5a: "validated schema selection on
    # one pinned transaction").
    assert "public" in ALLOWED_SCHEMA_NAMES


@pytest.mark.integration
async def test_schema_transaction_sets_and_resets_search_path(
    pg_conn: ferrum.connection.Connection,
    unique_suffix: str,
    require_native: None,
) -> None:
    """Manifest oai-01 (retargeted, live PG): ``schema_transaction`` opens a
    transaction, sets a transaction-local ``search_path`` to the validated
    schema, and the ``search_path`` resets on commit so the pooled connection
    does not leak tenant state. Mirrors the per-tenant schema selection that
    Onyx's ``schema_translate_map`` performs.
    """
    schema = f"cc_oai_tenant_{unique_suffix}"
    driver = pg_conn._require_driver()
    await driver.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
    try:
        allow = frozenset({schema, "public"})
        async with schema_transaction(pg_conn, schema, allowed_schemas=allow) as tx:
            sp = await tx._require_driver().fetchval("SELECT current_setting('search_path')")
            assert sp is not None
            assert schema in sp
        # After commit, the pooled connection's search_path has reset.
        default_sp = await driver.fetchval("SELECT current_setting('search_path')")
        assert schema not in default_sp
    finally:
        await driver.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


@pytest.mark.integration
async def test_schema_transaction_rejects_unregistered_schema(
    pg_conn: ferrum.connection.Connection,
    require_native: None,
) -> None:
    """Manifest oai-01 (retargeted): a schema identifier that is not in the
    allowlist fails with ``FerrumCompileError`` BEFORE opening the
    transaction. The default allowlist is ``{"public"}``; a valid-but-unlisted
    identifier is rejected — this is the structural safety property that
    replaces string interpolation of untrusted schema names.
    """
    with pytest.raises(FerrumCompileError) as exc_info:
        async with schema_transaction(pg_conn, "tenant_unlisted"):
            pass  # pragma: no cover
    assert exc_info.value.category == "schema_not_allowed"


@pytest.mark.integration
async def test_schema_transaction_rejects_injection_identifier(
    pg_conn: ferrum.connection.Connection,
    require_native: None,
) -> None:
    """Manifest oai-01 (retargeted): an SQL-injection attempt in the schema
    name is rejected by the identifier regex (``^[a-zA-Z_][a-zA-Z0-9_]{0,62}$``)
    BEFORE the allowlist check, even if the attacker-controlled value is
    artificially added to the allowlist. This is the no-raw-SQL §2.9 contract
    applied to schema selection.
    """
    with pytest.raises(FerrumCompileError) as exc_info:
        async with schema_transaction(
            pg_conn,
            "public; DROP TABLE x",
            allowed_schemas=frozenset({"public", "public; DROP TABLE x"}),
        ):
            pass  # pragma: no cover
    assert exc_info.value.category == "invalid_identifier"


# ---------------------------------------------------------------------------
# Retargeted oai-02: ShardRouter / ConnectionRegistry EXIST and work
# ---------------------------------------------------------------------------


def test_shard_router_registry_api_is_available() -> None:
    """Manifest oai-02 (retargeted): ``ConnectionRegistry`` and ``ShardRouter``
    are part of the public Ferrum surface (ratified W1-F contract, AGENTS.md
    §5a). The registry owns independently configured PostgreSQL pools; the
    router resolves a *trusted* shard key chosen by caller code to an
    explicit Connection/Transaction. QuerySet stays shard-unaware and
    connection-explicit.
    """
    assert hasattr(ferrum, "ShardRouter")
    assert hasattr(ferrum, "ConnectionRegistry")
    assert hasattr(ferrum.routing, "ShardRouter")
    assert hasattr(ferrum.routing, "ConnectionRegistry")
    # ConnectionRegistry is PostgreSQL-only by ratified contract: non-postgres
    # DSNs are rejected at registration time so the constraint is structural,
    # not by convention.
    with pytest.raises(FerrumConfigError):
        ConnectionRegistry({"a": PoolConfig(dsn="mysql://u@h/db")})


@pytest.mark.integration
async def test_shard_router_resolves_trusted_key_to_explicit_connection(
    pg_conn: ferrum.connection.Connection,
    pg_dsn: str,
    require_native: None,
) -> None:
    """Manifest oai-02 (retargeted, live PG): ``ConnectionRegistry`` opens
    independent PostgreSQL pools and ``ShardRouter`` resolves a *trusted*
    shard key via a caller-supplied resolver to an explicit Connection /
    Transaction. The router never inspects model metadata, tenant ids, or
    schema names to choose a connection — the resolver is the single place
    where routing policy lives.
    """
    registry = ConnectionRegistry(
        {
            "shard_a": PoolConfig(dsn=pg_dsn, max_size=2, application_name="cc_oai_a"),
            "shard_b": PoolConfig(dsn=pg_dsn, max_size=2, application_name="cc_oai_b"),
        }
    )
    await registry.start()
    try:
        router: ShardRouter[str] = ShardRouter(
            registry, resolver=lambda key: "shard_a" if key < "m" else "shard_b"
        )
        # Distinct keys resolve to distinct registered connections.
        conn_a = router.connection_for("alice")
        conn_b = router.connection_for("zoe")
        assert conn_a is not conn_b
        # Each resolved connection serves a query (proves the pool is live).
        assert await conn_a._require_driver().fetchval("SELECT 1") == 1
        assert await conn_b._require_driver().fetchval("SELECT 1") == 1
        # A transaction on the resolved shard commits.
        async with router.transaction_for("alice") as tx:
            assert await tx._require_driver().fetchval("SELECT 7") == 7
        # Resolver returning an unregistered shard name raises — the caller
        # cannot route to a connection that does not exist.
        bad_router: ShardRouter[str] = ShardRouter(registry, resolver=lambda key: "nope")
        with pytest.raises(FerrumConfigError):
            bad_router.connection_for("k")
    finally:
        await registry.close()
    # After close, get raises — no half-open registry state leaks.
    with pytest.raises(FerrumConfigError):
        registry.get("shard_a")


# ---------------------------------------------------------------------------
# Retargeted oai-03/oai-04: select_for_update EXISTS and works
# ---------------------------------------------------------------------------


def test_select_for_update_api_is_available() -> None:
    """Manifest oai-03/oai-04 (retargeted): ``QuerySet.select_for_update``
    is part of the public QuerySet surface (W1-B). It compiles a
    ``FOR UPDATE [OF ...] [NOWAIT|SKIP LOCKED]`` clause onto the SELECT.
    Mutually exclusive modifiers (``nowait=True`` AND ``skip_locked=True``)
    fail at compile time with a structured ``FerrumCompileError`` before
    SQL is emitted.
    """
    assert hasattr(QuerySet, "select_for_update")


def test_select_for_update_rejects_mutually_exclusive_modifiers() -> None:
    """Manifest oai-03/oai-04 (retargeted): ``nowait`` and ``skip_locked``
    together are rejected at compile time — PostgreSQL rejects both, and
    Ferrum fails fast with a structured ``FerrumCompileError`` before
    SQL is emitted (§3 SQL safety).
    """

    class Account(ferrum.Model):
        class Meta:
            table = "cc_oai_select_for_update_static"

        id: Annotated[int, ferrum.Field(primary_key=True)] = 0
        balance: int = 0

    with pytest.raises(FerrumCompileError):
        Account.objects.select_for_update(nowait=True, skip_locked=True)


@pytest.mark.integration
async def test_select_for_update_skip_locked_skips_locked_rows(
    pg_conn: ferrum.connection.Connection,
    unique_suffix: str,
    require_native: None,
) -> None:
    """Manifest oai-03 (retargeted, live PG): ``select_for_update(skip_locked=True)``
    lets a second worker race for queued rows without blocking on rows
    already locked by a peer — mirroring Onyx's
    ``_claim_next_processing_file()`` pattern (``.with_for_update(skip_locked=True)``).
    """
    item_table = f"cc_oai_for_update_skip_{unique_suffix}"

    class Item(ferrum.Model):
        class Meta:
            table = item_table

        id: Annotated[int, ferrum.Field(primary_key=True)]
        status: str = "pending"

    plan = json.dumps(
        {
            "name": f"cc_oai_for_update_skip_create_{unique_suffix}",
            "version": "1",
            "requires_confirmation": False,
            "ops": [
                ops.CreateTable(
                    item_table,
                    [
                        ops.Column("id", "BIGINT", not_null=True, primary_key=True),
                        ops.Column("status", "TEXT", not_null=True, default="''"),
                    ],
                ).to_op_dict()
            ],
        }
    )
    await apply(pg_conn, plan, dry_run=False)
    try:
        await Item.objects.create(pg_conn, id=1, status="pending")
        await Item.objects.create(pg_conn, id=2, status="pending")
        async with pg_conn.transaction() as tx1:
            # Hold a FOR UPDATE lock on id=1.
            locked = await Item.objects.filter(id=1).select_for_update().all(tx1)
            assert [r.id for r in locked] == [1]
            # A second transaction with SKIP LOCKED gets only id=2.
            async with pg_conn.transaction() as tx2:
                rows2 = await Item.objects.select_for_update(skip_locked=True).all(tx2)
                ids = {r.id for r in rows2}
                assert 2 in ids
                assert 1 not in ids
    finally:
        drop_plan = json.dumps(
            {
                "name": f"cc_oai_for_update_skip_drop_{unique_suffix}",
                "version": "1",
                "requires_confirmation": False,
                "ops": [ops.DropTable(item_table).to_op_dict()],
            }
        )
        await apply(pg_conn, drop_plan, dry_run=False, confirm=True)


@pytest.mark.integration
async def test_select_for_update_nowait_raises_lock_timeout(
    pg_conn: ferrum.connection.Connection,
    unique_suffix: str,
    require_native: None,
) -> None:
    """Manifest oai-04 (retargeted, live PG): ``select_for_update(nowait=True)``
    fails fast with ``FerrumTimeoutError`` (category ``lock_timeout``,
    SQLSTATE 55P03) when a row is already locked, instead of blocking —
    mirroring Onyx's ``document.py`` ``.with_for_update(nowait=True)`` pattern.
    """
    doc_table = f"cc_oai_for_update_nowait_{unique_suffix}"

    class Doc(ferrum.Model):
        class Meta:
            table = doc_table

        id: Annotated[int, ferrum.Field(primary_key=True)]
        title: str = ""

    plan = json.dumps(
        {
            "name": f"cc_oai_for_update_nowait_create_{unique_suffix}",
            "version": "1",
            "requires_confirmation": False,
            "ops": [
                ops.CreateTable(
                    doc_table,
                    [
                        ops.Column("id", "BIGINT", not_null=True, primary_key=True),
                        ops.Column("title", "TEXT", not_null=True, default="''"),
                    ],
                ).to_op_dict()
            ],
        }
    )
    await apply(pg_conn, plan, dry_run=False)
    try:
        await Doc.objects.create(pg_conn, id=1, title="doc-1")
        async with pg_conn.transaction() as tx1:
            await Doc.objects.filter(id=1).select_for_update().all(tx1)
            async with pg_conn.transaction() as tx2:
                with pytest.raises(FerrumTimeoutError) as exc_info:
                    await Doc.objects.filter(id=1).select_for_update(nowait=True).all(tx2)
                assert exc_info.value.category == "lock_timeout"
    finally:
        drop_plan = json.dumps(
            {
                "name": f"cc_oai_for_update_nowait_drop_{unique_suffix}",
                "version": "1",
                "requires_confirmation": False,
                "ops": [ops.DropTable(doc_table).to_op_dict()],
            }
        )
        await apply(pg_conn, drop_plan, dry_run=False, confirm=True)


# ---------------------------------------------------------------------------
# Encrypted / JSON codec contracts (W2-A) — key-provider + PII redaction
# ---------------------------------------------------------------------------


class _StaticKeyProvider:
    """Test key provider holding a fixed key per ``key_id`` in memory."""

    def __init__(self, keys: dict[str, bytes]) -> None:
        self._keys = keys

    def get_key(self, key_id: str) -> bytes:
        if key_id not in self._keys:
            raise FerrumCodecError(
                f"Key {key_id!r} not found",
                codec_kind="encrypted",
                key_id=key_id,
            )
        return self._keys[key_id]

    def key_ids(self) -> tuple[str, ...]:
        return tuple(self._keys.keys())


def test_encrypted_string_codec_round_trip_with_injected_key_provider() -> None:
    """Manifest oai-07/oai-08 codec contract (W2-A): ``EncryptedStringCodec``
    encrypts a string to bytes via a key supplied by an injected
    ``KeyProvider`` (never hardcoded), and decrypts back to the original
    string. The ciphertext is non-deterministic (random nonce), so the same
    plaintext produces different ciphertexts across calls.
    """
    key = secrets.token_bytes(32)
    provider = _StaticKeyProvider({"test-key": key})
    codec = EncryptedStringCodec(
        CodecMeta(kind="encrypted_string", key_id="test-key", pii=True),
        key_provider=provider,
    )
    plaintext = "SuperSecret SSN 123-45-6789"
    encrypted = codec.encode_bind(plaintext)
    assert isinstance(encrypted, bytes)
    assert encrypted != plaintext.encode("utf-8")
    # Non-deterministic: a second encryption produces a different ciphertext
    # (random nonce per encryption).
    assert codec.encode_bind(plaintext) != encrypted
    # Round-trip recovers the original plaintext.
    assert codec.decode_result(encrypted) == plaintext


def test_encrypted_json_codec_round_trip_with_injected_key_provider() -> None:
    """Manifest oai-07 codec contract (W2-A): ``EncryptedJSONCodec`` serializes
    a JSON-serializable value (dict, list) to JSON, encrypts it to bytes, and
    decrypts back to the original structure. Encryption is non-deterministic.
    """
    key = secrets.token_bytes(32)
    provider = _StaticKeyProvider({"j-key": key})
    codec = EncryptedJSONCodec(
        CodecMeta(kind="encrypted_json", key_id="j-key"),
        key_provider=provider,
    )
    data = {"name": "Alice", "age": 30, "items": [1, 2, 3], "nested": {"k": "v"}}
    encrypted = codec.encode_bind(data)
    assert isinstance(encrypted, bytes)
    assert codec.encode_bind(data) != encrypted  # random nonce
    assert codec.decode_result(encrypted) == data


def test_encrypted_codec_rejects_wrong_key_with_mac_failure() -> None:
    """Manifest oai-07 codec contract (W2-A): decrypting ciphertext with the
    wrong key fails authentication and raises ``FerrumCodecError`` — never
    returns plaintext. This is the encrypt-then-MAC authentication property.
    """
    key_a = secrets.token_bytes(32)
    key_b = secrets.token_bytes(32)
    provider_a = _StaticKeyProvider({"k": key_a})
    provider_b = _StaticKeyProvider({"k": key_b})
    codec_a = EncryptedStringCodec(
        CodecMeta(kind="encrypted_string", key_id="k"), key_provider=provider_a
    )
    codec_b = EncryptedStringCodec(
        CodecMeta(kind="encrypted_string", key_id="k"), key_provider=provider_b
    )
    encrypted = codec_a.encode_bind("secret")
    with pytest.raises(FerrumCodecError):
        codec_b.decode_result(encrypted)


def test_encrypted_codec_factory_requires_key_provider_at_query_time() -> None:
    """Manifest oai-07 codec contract (W2-A): the encrypted codec factory
    raises ``FerrumCodecError`` when no ``key_provider`` is supplied — keys
    are never hardcoded in model metadata; they are injected at query time.
    """
    from ferrum.models import _make_encrypted_string_factory

    factory = _make_encrypted_string_factory()
    with pytest.raises(FerrumCodecError) as exc_info:
        factory(CodecMeta(kind="encrypted_string", key_id="test-key"))
    assert exc_info.value.codec_kind == "encrypted_string"


def test_encrypted_codec_redact_never_exposes_plaintext_or_key() -> None:
    """Manifest oai-07 codec contract (W2-A / §3 credential handling): the
    codec's ``redact()`` method never returns the raw value, ciphertext, or
    key material — only a placeholder carrying the codec kind and key id for
    diagnostics. This is the canonical PII redaction path for logs, hooks,
    and error messages.
    """
    key = secrets.token_bytes(32)
    provider = _StaticKeyProvider({"redact-key": key})
    str_codec = EncryptedStringCodec(
        CodecMeta(kind="encrypted_string", key_id="redact-key", pii=True),
        key_provider=provider,
    )
    json_codec = EncryptedJSONCodec(
        CodecMeta(kind="encrypted_json", key_id="redact-key", pii=True),
        key_provider=provider,
    )
    plaintext = "PII value 42"
    redacted_str = str_codec.redact(plaintext)
    redacted_json = json_codec.redact({"k": "PII"})
    assert plaintext not in redacted_str
    assert plaintext not in redacted_json
    assert "PII" not in redacted_str
    assert "PII" not in redacted_json
    assert "redact-key" in redacted_str
    assert "redact-key" in redacted_json
    # The actual key bytes never appear in any redacted representation.
    key_hex = key.hex()
    assert key_hex not in redacted_str
    assert key_hex not in redacted_json


# ---------------------------------------------------------------------------
# Remaining oai entries — preserved as-is (defect / supported behavior proofs)
# ---------------------------------------------------------------------------


def test_nested_pydantic_model_field_falls_back_to_text_type_missing_api() -> None:
    """Manifest oai-06: annotating a field with a nested Pydantic BaseModel
    (Org AI Platform's PydanticType TypeDecorator use case) does not raise
    and does not map to JSONB — it silently falls back to a plain TEXT
    column, which is a wrong-DDL-type defect distinct from a clean rejection.
    """

    class NestedPayload(pydantic.BaseModel):
        key: str = ""

    class DocWithNestedPayload(ferrum.Model):
        id: Annotated[int, ferrum.Field(primary_key=True)]
        payload: NestedPayload = NestedPayload()

    metadata = DocWithNestedPayload.get_metadata()
    payload_field = next(f for f in metadata.fields if f.name == "payload")
    assert payload_field.field_type == "text"
    assert payload_field.sql_type == "TEXT"


@pytest.mark.integration
async def test_bulk_upsert_cannot_express_conditional_coalesce_update(
    pg_conn: ferrum.connection.Connection,
    unique_suffix: str,
    require_native: None,
) -> None:
    """Manifest oai-07: entities.py's transfer_entity
    ``on_conflict_do_update(set_=dict(entity_key=func.coalesce(KGEntity.entity_key,
    entity.entity_key), ...))`` cannot be expressed via bulk_upsert's static
    update_fields list — a new row's None always overwrites an existing
    non-null value.
    """
    kv_table = f"cc_oai_kv_{unique_suffix}"

    class Kv(ferrum.Model):
        class Meta:
            table = kv_table

        key: Annotated[str, ferrum.Field(primary_key=True)]
        value: str | None = None

    plan = json.dumps(
        {
            "name": f"cc_oai_kv_create_{unique_suffix}",
            "version": "1",
            "requires_confirmation": False,
            "ops": [
                ops.CreateTable(
                    kv_table,
                    [
                        ops.Column("key", "TEXT", not_null=True, primary_key=True),
                        ops.Column("value", "TEXT"),
                    ],
                ).to_op_dict()
            ],
        }
    )
    await apply(pg_conn, plan, dry_run=False)
    try:
        await Kv.objects.create(pg_conn, key="k1", value="original")

        incoming = Kv.model_construct(key="k1", value=None)
        await Kv.objects.bulk_upsert(
            pg_conn,
            [incoming],
            conflict_fields=["key"],
            update_fields=["value"],
            returning=False,
        )

        fetched = await Kv.objects.filter(key="k1").get(pg_conn)
        # This is the gap: a real COALESCE-based upsert would have preserved
        # "original" when the incoming value is None. Ferrum's bulk_upsert
        # always overwrites with the incoming value.
        assert fetched.value is None
    finally:
        drop_plan = json.dumps(
            {
                "name": f"cc_oai_kv_drop_{unique_suffix}",
                "version": "1",
                "requires_confirmation": False,
                "ops": [ops.DropTable(kv_table).to_op_dict()],
            }
        )
        await apply(pg_conn, drop_plan, dry_run=False, confirm=True)


@pytest.mark.integration
async def test_detect_drift_compares_a_named_non_public_schema(
    pg_conn: ferrum.connection.Connection,
    unique_suffix: str,
    require_native: None,
) -> None:
    """Manifest oai-10: detect_drift(conn, models, schema=<tenant schema>)
    correctly reports missing/extra columns for a table created outside
    'public' — the read-only fidelity-check primitive Onyx's Alembic
    env.py (include_schemas=True + per-tenant schema selection) would need
    for a per-shard drift check, independent of Ferrum's numbered-SQL-only
    migration-apply policy.
    """
    schema_name = f"cc_oai_tenant_{unique_suffix}"
    table_name = f"cc_oai_doc_{unique_suffix}"
    driver = pg_conn._require_driver()
    await driver.execute(f'CREATE SCHEMA "{schema_name}"')
    try:
        # Live table has "legacy_col" (unknown to the model) and omits
        # "extra_field" (declared on the model but absent live).
        await driver.execute(
            f'CREATE TABLE "{schema_name}"."{table_name}" ('
            f"id BIGINT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', "
            f"legacy_col TEXT NOT NULL DEFAULT ''"
            f")"
        )

        class Doc(ferrum.Model):
            class Meta:
                table = table_name

            id: Annotated[int, ferrum.Field(primary_key=True)]
            name: str = ""
            extra_field: str = ""

        report = await detect_drift(pg_conn, [Doc], schema=schema_name)
        assert report.has_drift is True
        assert report.missing_columns.get(table_name) == ("extra_field",)
        assert report.extra_columns.get(table_name) == ("legacy_col",)
    finally:
        await driver.execute(f'DROP SCHEMA "{schema_name}" CASCADE')
