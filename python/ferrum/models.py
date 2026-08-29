"""Ferrum model base class and Pydantic v2 metadata builder.

``Model`` extends Pydantic's ``BaseModel`` with a ``ModelConfig``-driven metadata
builder that runs once at class definition time. The produced ``ModelMetadata`` is
immutable and shared read-only across all queries for that class.

Design constraints:
- No SQL string building here. Models only produce the IR and the metadata struct.
- Field validation and serialization are Pydantic's responsibility.
- ``ModelMetadata`` is the single source of truth for the allowlists used by the
  Rust compiler; it must never be mutated after class construction (AGENTS.md §2.10).
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import struct
import types as _types
from collections.abc import Callable
from datetime import date, datetime, time
from decimal import Decimal
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    ClassVar,
    Literal,
    NoReturn,
    Protocol,
    TypeVar,
    Union,
    cast,
    get_args,
    get_origin,
    overload,
)
from uuid import UUID

from pydantic import BaseModel as _PydanticBaseModel
from pydantic import ConfigDict
from pydantic import Field as _PydanticField
from pydantic_core import core_schema

from ferrum.errors import FerrumError

# ---------------------------------------------------------------------------
# Type mapping: Python annotation → Ferrum field type string (DATA_MODELING.md §3.2)
# ---------------------------------------------------------------------------
_SUPPORTED_TYPES: dict[type, str] = {
    int: "int",
    str: "text",
    bool: "bool",
    float: "float",
    Decimal: "decimal",
    datetime: "datetime",
    date: "date",
    time: "time",
    UUID: "uuid",
    bytes: "bytes",
    dict: "json",
    list: "array_text",  # bare list -> text[]; list[T] in _build_metadata
}


class Vector:
    """Sentinel type for pgvector ``VECTOR(n)`` columns.

    Use with ``Field(vector_dimensions=n)``::

        embedding: Vector = Field(vector_dimensions=1536)
    """

    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        source: Any,  # noqa: ANN401
        handler: Any,  # noqa: ANN401
    ) -> core_schema.CoreSchema:
        del source, handler
        return core_schema.list_schema(items_schema=core_schema.float_schema())


class TSVector:
    """Sentinel type for PostgreSQL ``TSVECTOR`` full-text search columns."""

    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        source: Any,  # noqa: ANN401
        handler: Any,  # noqa: ANN401
    ) -> core_schema.CoreSchema:
        del source, handler
        return core_schema.str_schema()


_SUPPORTED_TYPES[Vector] = "vector"
_SUPPORTED_TYPES[TSVector] = "tsvector"

# ---------------------------------------------------------------------------
# Operator allowlists per Ferrum field type (QUERY_ENGINE.md §4.2)
# ---------------------------------------------------------------------------
_ALLOWED_OPERATORS: dict[str, tuple[str, ...]] = {
    "int": ("eq", "gt", "gte", "lt", "lte", "in", "is_null", "is_not_null", "ne", "range"),
    "big_int": ("eq", "gt", "gte", "lt", "lte", "in", "is_null", "is_not_null", "ne", "range"),
    "text": (
        "eq",
        "iexact",
        "contains",
        "icontains",
        "startswith",
        "endswith",
        "istartswith",
        "iendswith",
        "in",
        "is_null",
        "is_not_null",
        "ne",
    ),
    "bool": ("eq", "is_null", "is_not_null", "ne"),
    "float": ("eq", "gt", "gte", "lt", "lte", "in", "is_null", "is_not_null", "ne", "range"),
    "decimal": ("eq", "gt", "gte", "lt", "lte", "in", "is_null", "is_not_null", "ne", "range"),
    "datetime": ("eq", "gt", "gte", "lt", "lte", "is_null", "is_not_null", "ne", "range"),
    "date": ("eq", "gt", "gte", "lt", "lte", "is_null", "is_not_null", "ne", "range"),
    "time": ("eq", "gt", "gte", "lt", "lte", "is_null", "is_not_null", "ne", "range"),
    "uuid": ("eq", "in", "is_null", "is_not_null", "ne"),
    "bytes": ("eq", "in", "is_null", "is_not_null", "ne"),
    # JSONB: containment + key existence operators (PostgreSQL @>, ?, ?|)
    "json": ("eq", "is_null", "is_not_null", "contains", "has_key", "has_any_keys"),
    "vector": ("is_null", "is_not_null"),
    "tsvector": (
        "match",
        "match_phrase",
        "match_websearch",
        "match_boolean",
        "is_null",
        "is_not_null",
    ),
    # PostgreSQL array types — array containment and overlap operators
    "array_text": ("eq", "is_null", "is_not_null", "contains", "contained_by", "overlap"),
    "array_int": ("eq", "is_null", "is_not_null", "contains", "contained_by", "overlap"),
    "array_uuid": ("eq", "is_null", "is_not_null", "contains", "contained_by", "overlap"),
    "array_float": ("eq", "is_null", "is_not_null", "contains", "contained_by", "overlap"),
    # Enum (TEXT + CHECK constraint) — equality and membership operators
    "enum": ("eq", "is_null", "is_not_null", "ne", "in"),
    # PostgreSQL CITEXT — case-insensitive text (same operators as text)
    "citext": (
        "eq",
        "iexact",
        "contains",
        "icontains",
        "startswith",
        "endswith",
        "istartswith",
        "iendswith",
        "in",
        "is_null",
        "is_not_null",
        "ne",
    ),
    # PostgreSQL INET — network address (eq, membership, containment)
    "inet": ("eq", "in", "is_null", "is_not_null", "ne", "contains", "contained_by"),
    # PostgreSQL custom domain — inherits base type operators
    "domain": ("eq", "is_null", "is_not_null", "ne"),
}

# Allowlist for ON DELETE actions in FK constraints (SQL injection guard).
_ON_DELETE_ALLOWLIST: frozenset[str] = frozenset(
    {"CASCADE", "SET NULL", "RESTRICT", "SET DEFAULT", "NO ACTION"}
)


def _to_snake_case(name: str) -> str:
    """Convert CamelCase class name to snake_case table name."""
    s1 = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s1).lower()


def _unwrap_optional(annotation: Any) -> tuple[Any, bool]:  # noqa: ANN401
    """Return (inner_type, is_nullable) unwrapping ``T | None`` / ``Optional[T]``."""
    origin = get_origin(annotation)
    is_union = origin is Union
    # Python 3.10+ union syntax ``T | None`` uses types.UnionType at runtime.
    if not is_union and hasattr(_types, "UnionType") and isinstance(annotation, _types.UnionType):
        is_union = True
    if is_union:
        args = get_args(annotation)
        non_none = [a for a in args if a is not type(None)]
        if non_none:
            return non_none[0], True
    return annotation, False


# ---------------------------------------------------------------------------
# Immutable metadata dataclasses (DATA_MODELING.md §4)
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class FieldMeta:
    """Immutable descriptor for a single model field."""

    name: str
    column_name: str
    python_type_name: str
    field_type: str
    allowed_operators: tuple[str, ...]
    nullable: bool
    pk: bool
    max_length: int | None = None
    max_digits: int | None = None
    decimal_places: int | None = None
    unique: bool = False
    db_index: bool = False
    db_default: str | None = None
    python_default: Any | None = None
    vector_dimensions: int | None = None
    # For enum fields: the allowed string values (drives CHECK constraint in DDL).
    enum_values: tuple[str, ...] | None = None
    # Full-text search metadata (Wave 0 / ADR-007).
    fts_config: str | None = None
    fts_source_columns: tuple[str, ...] | None = None
    # JSONB-backed list fields retain their list-shaped Python contract while
    # remaining distinct from PostgreSQL ARRAY metadata.
    jsonb_list: bool = False
    # Database-generated and read-only columns participate in SELECT/hydration
    # metadata but are excluded from write metadata.
    generated: bool = False
    read_only: bool = False
    # Codec metadata — immutable, IDE-visible, migration-aware.
    # The runtime FieldCodec is resolved from this at query time (W2-B).
    codec_meta: CodecMeta | None = None
    # PostgreSQL domain name (for field_type="domain" DDL emission).
    domain_name: str | None = None

    @property
    def sql_type(self) -> str:
        """PostgreSQL DDL type string derived from field_type and constraints."""
        return _field_type_to_sql(self)

    @property
    def writable(self) -> bool:
        """Whether callers may include this field in persistence writes."""
        return not self.generated and not self.read_only


@dataclasses.dataclass(frozen=True)
class IndexMeta:
    """Immutable descriptor for a declarative model index (``Meta.indexes``)."""

    name: str
    fields: tuple[str, ...]
    unique: bool = False
    using: str = "btree"
    where: str | None = None


@dataclasses.dataclass(frozen=True)
class FullTextIndex:
    """Declarative full-text index for ``class Meta: full_text_indexes = [...]``."""

    fields: tuple[str, ...]
    name: str | None = None
    config: str | None = None
    sqlite_content_table: str | None = None


@dataclasses.dataclass(frozen=True)
class FullTextIndexMeta:
    """Immutable descriptor for a model-level full-text index."""

    name: str
    fields: tuple[str, ...]
    config: str | None = None
    sqlite_content_table: str | None = None


@dataclasses.dataclass(frozen=True)
class Index:
    """Declarative index definition for ``class Meta: indexes = [...]``."""

    fields: tuple[str, ...]
    name: str | None = None
    unique: bool = False
    using: str = "btree"
    where: str | None = None


@dataclasses.dataclass(frozen=True)
class RelationMeta:
    """Immutable descriptor for a single relationship field.

    Populated once during ``_build_metadata`` and stored on ``ModelMetadata``.
    Never carries bound values, row data, or connection info.
    """

    field_name: str
    kind: str  # "fk" | "one_to_one" | "m2m"
    to_model: str
    db_column: str | None = None  # backing FK column for fk/one_to_one
    through_table: str | None = None  # join table for m2m
    on_delete: str | None = None  # validated ON DELETE action
    related_name: str | None = None


@dataclasses.dataclass(frozen=True)
class ModelMetadata:
    """Immutable model metadata built once at class-definition time.

    Serves as the allowlist source for the Rust compiler and the migration
    planner. Never carries connection info, bound values, or row data (DM-7).
    Shared read-only across async tasks — no locks required (ARCHITECTURE §6.3).

    ``pk_fields`` is the canonical tuple of field indices for the primary key.
    ``pk_index`` is kept as a backward-compat property returning ``pk_fields[0]``.
    For single-PK models the two are equivalent; for composite-PK models
    ``pk_fields`` contains all participating indices.
    """

    table_name: str
    model_name: str
    fields: tuple[FieldMeta, ...]
    indexes: tuple[IndexMeta, ...] = ()
    full_text_indexes: tuple[FullTextIndexMeta, ...] = ()
    allowed_sort_directions: tuple[str, ...] = ("asc", "desc")
    # pk_fields: all PK field indices in definition order.
    # Defaults to (0,) so the first field is implicitly the PK when not specified.
    pk_fields: tuple[int, ...] = (0,)
    relations: tuple[RelationMeta, ...] = ()

    @property
    def pk_index(self) -> int:
        """Backward-compat alias: index of the *first* PK field."""
        return self.pk_fields[0] if self.pk_fields else 0

    @property
    def writable_fields(self) -> tuple[FieldMeta, ...]:
        """Fields accepted by create/update/upsert/bulk write paths."""
        return tuple(field for field in self.fields if field.writable)

    @property
    def writable_field_names(self) -> frozenset[str]:
        """Immutable field-name allowlist for persistence writes."""
        return frozenset(field.name for field in self.writable_fields)

    def to_metadata_dict(self) -> dict[str, Any]:
        """Build the ``ModelMetadata`` payload dict Rust's serde deserializer expects.

        Shared by the JSON (:meth:`to_metadata_json`) and MessagePack boundary
        paths. Field ``pk`` is Python-internal and is not sent across the
        boundary. Emits both ``pk_index`` (legacy, first PK) and ``pk_fields``
        (all PK indices) so the Rust side can use either depending on the operation.
        """
        field_payloads: list[dict[str, Any]] = []
        for f in self.fields:
            payload: dict[str, Any] = {
                "name": f.name,
                "column_name": f.column_name,
                "field_type": f.field_type,
                "allowed_operators": list(f.allowed_operators),
                "nullable": f.nullable,
            }
            if f.vector_dimensions is not None:
                payload["vector_dimensions"] = f.vector_dimensions
            if f.fts_config is not None:
                payload["fts_config"] = f.fts_config
            if f.fts_source_columns is not None:
                payload["fts_source_columns"] = list(f.fts_source_columns)
            if f.jsonb_list:
                payload["jsonb_list"] = True
            if f.generated:
                payload["generated"] = True
            if f.read_only:
                payload["read_only"] = True
            field_payloads.append(payload)
        fts_payloads = [
            {
                "name": idx.name,
                "fields": list(idx.fields),
                **({"config": idx.config} if idx.config is not None else {}),
            }
            for idx in self.full_text_indexes
        ]
        return {
            "model_name": self.model_name,
            "table_name": self.table_name,
            "pk_index": self.pk_index,
            "pk_fields": list(self.pk_fields),
            "fields": field_payloads,
            "full_text_indexes": fts_payloads,
        }

    def to_metadata_json(self) -> str:
        """Serialize to the JSON string expected by ``ferrum._native.compile_query``.

        Produces the ``ModelMetadata`` shape that Rust's serde deserializer
        expects (ADR-002 §ModelMetadata.fields).
        """
        return json.dumps(self.to_metadata_dict())

    def to_write_metadata_dict(self) -> dict[str, Any]:
        """Build metadata restricted to fields accepted by write operations.

        Primary-key indices are remapped to the filtered field tuple. A
        generated primary key therefore has no write-side PK index, while the
        full metadata remains available for reads and hydration.
        """
        payload = self.to_metadata_dict()
        payload["fields"] = [
            field_payload
            for field, field_payload in zip(self.fields, payload["fields"], strict=True)
            if field.writable
        ]
        writable_positions = {
            original_index: write_index
            for write_index, (original_index, field) in enumerate(
                item for item in enumerate(self.fields) if item[1].writable
            )
        }
        write_pk_fields = [
            writable_positions[index] for index in self.pk_fields if index in writable_positions
        ]
        payload["pk_fields"] = write_pk_fields
        payload["pk_index"] = write_pk_fields[0] if write_pk_fields else 0
        return payload


# ---------------------------------------------------------------------------
# DDL type helper
# ---------------------------------------------------------------------------


def _field_type_to_sql(field: FieldMeta) -> str:
    """Return the PostgreSQL DDL type string for a FieldMeta.

    This is a DDL-only concern — the query IR path uses the ``field_type``
    string tags (``"text"``, ``"int"``, etc.) unchanged regardless of DDL type.
    """
    ft = field.field_type
    if ft == "text":
        if field.max_length is not None:
            return f"VARCHAR({field.max_length})"
        return "TEXT"
    if ft == "int":
        return "INTEGER"
    if ft == "big_int":
        return "BIGSERIAL" if field.pk else "BIGINT"
    if ft == "float":
        return "REAL"
    if ft == "decimal":
        if field.max_digits is not None and field.decimal_places is not None:
            return f"NUMERIC({field.max_digits},{field.decimal_places})"
        return "NUMERIC"
    if ft == "bool":
        return "BOOLEAN"
    if ft == "datetime":
        return "TIMESTAMPTZ"
    if ft == "date":
        return "DATE"
    if ft == "time":
        return "TIME"
    if ft == "uuid":
        return "UUID"
    if ft == "bytes":
        return "BYTEA"
    if ft == "json":
        return "JSONB"
    if ft == "vector":
        if field.vector_dimensions is None:
            raise ValueError(f"Vector field {field.name!r} requires vector_dimensions on Field().")
        return f"VECTOR({field.vector_dimensions})"
    if ft == "tsvector":
        return "TSVECTOR"
    # PostgreSQL array types
    if ft == "array_text":
        return "TEXT[]"
    if ft == "array_int":
        return "INTEGER[]"
    if ft == "array_uuid":
        return "UUID[]"
    if ft == "array_float":
        return "FLOAT8[]"
    # Enum: stored as TEXT; CHECK constraint is emitted by the migration orchestrator
    # when ``field.enum_values`` is populated.
    if ft == "enum":
        return "TEXT"
    # PostgreSQL CITEXT (requires the citext extension)
    if ft == "citext":
        return "CITEXT"
    # PostgreSQL INET (network address)
    if ft == "inet":
        return "INET"
    # PostgreSQL custom domain — emits the domain name directly
    if ft == "domain":
        if field.domain_name is None:
            raise ValueError(f"Domain field {field.name!r} requires domain_name on Field().")
        return field.domain_name
    return "TEXT"


# ---------------------------------------------------------------------------
# Field codecs — typed bind/result conversion (W2-A)
#
# The FieldCodec contract is Python-side only. It never does I/O and never
# touches the Rust core. Codec metadata (CodecMeta) is immutable, stored in
# FieldMeta at class-definition time, and excluded from hook payloads, logs,
# and error messages (PII redaction). The runtime codec is resolved from
# CodecMeta at query time by the QuerySet hydration path (W2-B).
# ---------------------------------------------------------------------------

# Nonce size for encrypted codecs (16 bytes — NIST-recommended minimum).
_ENC_NONCE_SIZE: int = 16
# MAC size for encrypted codecs (32 bytes — SHA-256 output).
_ENC_MAC_SIZE: int = 32
# Required key size for encrypted codecs (32 bytes — SHA-256 key length).
_ENC_KEY_SIZE: int = 32


class FerrumCodecError(FerrumError):
    """Field codec conversion or encryption failure.

    Raised when a codec cannot encode/decode a value, when encrypted
    ciphertext is malformed or tampered, or when a key provider fails.

    The error message never includes the raw value (PII redaction).
    Codec metadata (key_id, codec kind) may appear for diagnostics.
    """

    code = "FERR-C200"
    category = "internal"

    def __init__(
        self,
        message: str,
        *,
        codec_kind: str | None = None,
        key_id: str | None = None,
        model: str | None = None,
        field: str | None = None,
    ) -> None:
        super().__init__(message, category="internal", model=model)
        self.codec_kind = codec_kind
        self.key_id = key_id
        self.field = field


@dataclasses.dataclass(frozen=True)
class CodecMeta:
    """Immutable codec metadata stored in :class:`FieldMeta`.

    Built once at class-definition time and shared read-only across all
    async tasks. Never carries key material, key providers, or runtime
    context — those are resolved at query time by the QuerySet (W2-B).

    Attributes:
        kind: Codec kind string (``"encrypted_string"``, ``"nested_model"``,
            ``"citext"``, ``"inet"``, ``"bytea"``, ``"enum"``, ``"vector"``,
            ``"domain"``, ``"array"``, ``"passthrough"``).
        pii: Whether this field contains PII. When ``True``, the codec's
            ``redact()`` method is the canonical redaction path for logs,
            hooks, and error messages.
        key_id: Key identifier for encrypted codecs. Used for key rotation —
            the key provider resolves ``key_id`` to the actual key at query
            time. Never the key itself.
        model_class_name: Fully-qualified model class name for nested-model
            JSONB codecs. Resolved from the model registry at query time.
        domain_name: PostgreSQL domain name for domain codecs (DDL emission).
        element_type: Element type tag for array codecs (``"text"``,
            ``"int"``, ``"uuid"``, ``"float"``).
    """

    kind: str
    pii: bool = False
    key_id: str | None = None
    model_class_name: str | None = None
    domain_name: str | None = None
    element_type: str | None = None


class KeyProvider(Protocol):
    """Injectable key provider for encrypted codecs.

    Key providers supply encryption keys by ``key_id`` at query time.
    They never appear in model metadata, hook payloads, or error
    messages. A key provider may be backed by a KMS, environment
    variables, vault, or any secret-management system.

    Required protocol:
        - ``get_key(key_id)``: Return the raw key bytes for the given key id.
          Must raise :class:`FerrumCodecError` when the key is unavailable.
        - ``key_ids()``: Return the set of available key ids. Never returns
          key material — only identifiers.

    Security contract:
        - Keys must be exactly 32 bytes (SHA-256 key length).
        - The provider must never log, cache in plaintext, or expose keys
          in exception messages.
        - Key rotation: return different keys for the same ``key_id`` over
          time; the codec re-encrypts on write and accepts old ciphertext
          on read until all rows are migrated.
    """

    def get_key(self, key_id: str) -> bytes:
        """Return the 32-byte encryption key for the given key id.

        Raises:
            FerrumCodecError: When the key is unavailable or invalid.
        """
        ...

    def key_ids(self) -> tuple[str, ...]:
        """Return the tuple of available key ids. Never key material."""
        ...


class FieldCodec(Protocol):
    """Typed contract for field bind/result conversion.

    The codec is a **pure function pair** — it converts between Python
    values and driver-bindable values without any I/O, async, or state
    mutation. Encrypted codecs receive their key via a :class:`KeyProvider`
    at construction time; the codec itself is stateless and immutable.

    The codec is resolved from :class:`CodecMeta` at query time by the
    QuerySet hydration path (W2-B). This keeps model metadata immutable
    and allows key rotation without model redefinition.

    Attributes:
        codec_meta: The immutable :class:`CodecMeta` describing this codec.

    Methods:
        encode_bind: Convert a Python value to a driver-bindable value.
        decode_result: Convert a DB-returned value to a Python value.
        redact: Return a redacted representation for logs/hooks/errors.
    """

    codec_meta: CodecMeta

    def encode_bind(self, value: Any) -> Any:  # noqa: ANN401
        """Convert a Python value to a driver-bindable value.

        This is a pure function — no I/O, no async, no side effects.
        For encrypted codecs, this performs encryption using the injected
        key. For nested-model codecs, this serializes the Pydantic model
        to a dict. The returned value is what asyncpg binds as a parameter.

        Raises:
            FerrumCodecError: When the value cannot be encoded.
        """
        ...

    def decode_result(self, value: Any) -> Any:  # noqa: ANN401
        """Convert a DB-returned value to a Python value.

        This is a pure function — no I/O, no async, no side effects.
        For encrypted codecs, this performs decryption and authentication.
        For nested-model codecs, this constructs a Pydantic model from the
        dict using ``model_construct`` (trusted DB fast path, ADR-003).

        Raises:
            FerrumCodecError: When the value cannot be decoded (malformed
                ciphertext, authentication failure, wrong key).
        """
        ...

    def redact(self, value: Any) -> str:  # noqa: ANN401
        """Return a redacted representation for logs, hooks, and errors.

        Never returns the raw value. For PII fields (``codec_meta.pii``
        is ``True``), this is the canonical redaction path. For non-PII
        fields, returns a type-appropriate placeholder.

        The redacted string never contains key material, ciphertext, or
        plaintext PII. It may contain the codec kind and key id for
        diagnostics.
        """
        ...


# ---------------------------------------------------------------------------
# Encryption primitives (stdlib-only, authenticated encryption)
#
# Encrypt-then-MAC with random nonce:
#   nonce = secrets.token_bytes(16)
#   enc_key, mac_key = HMAC-SHA256(master_key, nonce)
#   keystream = SHA-256(enc_key || counter) for counter in 0, 1, 2, ...
#   ciphertext = XOR(plaintext, keystream)
#   mac = HMAC-SHA256(mac_key, nonce || ciphertext)
#   output = nonce || mac || ciphertext
#
# This is a legitimate authenticated encryption construction using only
# the Python standard library. It is NOT AES, but it is cryptographically
# sound as long as SHA-256 is collision-resistant.
# ---------------------------------------------------------------------------


def _derive_keys(master_key: bytes, nonce: bytes) -> tuple[bytes, bytes]:
    """Derive separate encryption and MAC keys from the master key + nonce."""
    enc_key = hmac.new(master_key, b"enc:" + nonce, hashlib.sha256).digest()
    mac_key = hmac.new(master_key, b"mac:" + nonce, hashlib.sha256).digest()
    return enc_key, mac_key


def _keystream(enc_key: bytes, length: int) -> bytes:
    """Generate a keystream using SHA-256 in counter mode."""
    result = bytearray()
    counter = 0
    while len(result) < length:
        block = hashlib.sha256(enc_key + struct.pack(">I", counter)).digest()
        result.extend(block)
        counter += 1
    return bytes(result[:length])


def _xor_bytes(data: bytes, key: bytes) -> bytes:
    """XOR data with a keystream of equal or greater length."""
    return bytes(a ^ b for a, b in zip(data, key, strict=False))


def _encrypt(plaintext: bytes, key: bytes) -> bytes:
    """Encrypt plaintext with key. Returns nonce || mac || ciphertext."""
    if len(key) != _ENC_KEY_SIZE:
        raise FerrumCodecError(
            f"Encryption key must be {_ENC_KEY_SIZE} bytes, got {len(key)}.",
            codec_kind="encrypted",
        )
    nonce = secrets.token_bytes(_ENC_NONCE_SIZE)
    enc_key, mac_key = _derive_keys(key, nonce)
    ks = _keystream(enc_key, len(plaintext))
    ciphertext = _xor_bytes(plaintext, ks)
    mac = hmac.new(mac_key, nonce + ciphertext, hashlib.sha256).digest()
    return nonce + mac + ciphertext


def _decrypt(data: bytes, key: bytes) -> bytes:
    """Decrypt data (nonce || mac || ciphertext) with key. Returns plaintext."""
    if len(key) != _ENC_KEY_SIZE:
        raise FerrumCodecError(
            f"Encryption key must be {_ENC_KEY_SIZE} bytes, got {len(key)}.",
            codec_kind="encrypted",
        )
    min_len = _ENC_NONCE_SIZE + _ENC_MAC_SIZE
    if len(data) < min_len:
        raise FerrumCodecError(
            f"Ciphertext too short: expected >= {min_len} bytes, got {len(data)}.",
            codec_kind="encrypted",
        )
    nonce = data[:_ENC_NONCE_SIZE]
    mac = data[_ENC_NONCE_SIZE : _ENC_NONCE_SIZE + _ENC_MAC_SIZE]
    ciphertext = data[_ENC_NONCE_SIZE + _ENC_MAC_SIZE :]
    enc_key, mac_key = _derive_keys(key, nonce)
    expected_mac = hmac.new(mac_key, nonce + ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(mac, expected_mac):
        raise FerrumCodecError(
            "Ciphertext authentication failed: data was tampered or key is incorrect.",
            codec_kind="encrypted",
        )
    ks = _keystream(enc_key, len(ciphertext))
    return _xor_bytes(ciphertext, ks)


# ---------------------------------------------------------------------------
# Concrete codec implementations
# ---------------------------------------------------------------------------


class PassthroughCodec:
    """Identity codec — passes values through unchanged.

    Used for bytea, arrays, enums, and other types where asyncpg handles
    the conversion natively. The codec metadata is still stored in
    FieldMeta for migration awareness and IDE visibility.
    """

    def __init__(self, codec_meta: CodecMeta) -> None:
        self.codec_meta = codec_meta

    def encode_bind(self, value: Any) -> Any:  # noqa: ANN401
        return value

    def decode_result(self, value: Any) -> Any:  # noqa: ANN401
        return value

    def redact(self, value: Any) -> str:  # noqa: ANN401
        if self.codec_meta.pii:
            return f"[REDACTED:pii:{self.codec_meta.kind}]"
        return f"[passthrough:{self.codec_meta.kind}]"


class NestedModelCodec:
    """JSONB codec for nested Pydantic models.

    Serializes a Pydantic model instance to a dict for binding (asyncpg
    encodes the dict as JSONB). On decode, constructs the model from the
    dict using ``model_construct`` (trusted DB fast path, ADR-003 — no
    revalidation since the DB already enforced types).

    The model class is resolved from the Ferrum model registry at
    construction time by the caller (W2-B). This codec is constructed with
    the resolved class.
    """

    def __init__(self, codec_meta: CodecMeta, *, model_cls: type[_PydanticBaseModel]) -> None:
        self.codec_meta = codec_meta
        self._model_cls = model_cls

    def encode_bind(self, value: Any) -> Any:  # noqa: ANN401
        if value is None:
            return None
        if isinstance(value, _PydanticBaseModel):
            return value.model_dump(mode="json")
        if isinstance(value, dict):
            return value
        raise FerrumCodecError(
            f"NestedModelCodec expected a Pydantic model or dict, got {type(value).__name__}.",
            codec_kind="nested_model",
        )

    def decode_result(self, value: Any) -> Any:  # noqa: ANN401
        if value is None:
            return None
        if isinstance(value, str):
            value = json.loads(value)
        if not isinstance(value, dict):
            raise FerrumCodecError(
                f"NestedModelCodec expected a dict from JSONB, got {type(value).__name__}.",
                codec_kind="nested_model",
            )
        return self._model_cls.model_construct(**value)

    def redact(self, value: Any) -> str:  # noqa: ANN401
        if self.codec_meta.pii:
            return f"[REDACTED:pii:nested_model:{self._model_cls.__name__}]"
        return f"[nested_model:{self._model_cls.__name__}]"


class NestedListModelCodec:
    """JSONB codec for a list of nested Pydantic models.

    Serializes a list of Pydantic model instances to a list of dicts for
    binding. On decode, constructs each model from its dict using
    ``model_construct`` (trusted DB fast path, ADR-003).
    """

    def __init__(self, codec_meta: CodecMeta, *, model_cls: type[_PydanticBaseModel]) -> None:
        self.codec_meta = codec_meta
        self._model_cls = model_cls

    def encode_bind(self, value: Any) -> Any:  # noqa: ANN401
        if value is None:
            return None
        if not isinstance(value, list):
            raise FerrumCodecError(
                f"NestedListModelCodec expected a list, got {type(value).__name__}.",
                codec_kind="nested_list",
            )
        result: list[Any] = []
        for item in value:
            if isinstance(item, _PydanticBaseModel):
                result.append(item.model_dump(mode="json"))
            elif isinstance(item, dict):
                result.append(item)
            else:
                raise FerrumCodecError(
                    f"NestedListModelCodec list items must be models or dicts, "
                    f"got {type(item).__name__}.",
                    codec_kind="nested_list",
                )
        return result

    def decode_result(self, value: Any) -> Any:  # noqa: ANN401
        if value is None:
            return None
        if isinstance(value, str):
            value = json.loads(value)
        if not isinstance(value, list):
            raise FerrumCodecError(
                f"NestedListModelCodec expected a list from JSONB, got {type(value).__name__}.",
                codec_kind="nested_list",
            )
        return [
            self._model_cls.model_construct(**item) if isinstance(item, dict) else item
            for item in value
        ]

    def redact(self, value: Any) -> str:  # noqa: ANN401
        if self.codec_meta.pii:
            return f"[REDACTED:pii:nested_list:{self._model_cls.__name__}]"
        return f"[nested_list:{self._model_cls.__name__}]"


class EncryptedStringCodec:
    """Encrypted string codec with key-provider injection.

    Encrypts string values to bytes (BYTEA storage) and decrypts bytes
    back to strings. Uses authenticated encryption (encrypt-then-MAC)
    with a random nonce per encryption. The key is supplied by a
    :class:`KeyProvider` at construction time — never hardcoded.

    Key rotation: the codec encrypts with the current key (from the key
    provider for ``codec_meta.key_id``). Decryption verifies the MAC
    and fails with :class:`FerrumCodecError` when the key is wrong.
    """

    def __init__(self, codec_meta: CodecMeta, *, key_provider: KeyProvider) -> None:
        if codec_meta.key_id is None:
            raise FerrumCodecError(
                "EncryptedStringCodec requires codec_meta.key_id.",
                codec_kind="encrypted_string",
            )
        self.codec_meta = codec_meta
        self._key_provider = key_provider

    def encode_bind(self, value: Any) -> Any:  # noqa: ANN401
        if value is None:
            return None
        if not isinstance(value, str):
            raise FerrumCodecError(
                f"EncryptedStringCodec expected str, got {type(value).__name__}.",
                codec_kind="encrypted_string",
                key_id=self.codec_meta.key_id,
            )
        plaintext = value.encode("utf-8")
        key = self._key_provider.get_key(self.codec_meta.key_id or "")
        return _encrypt(plaintext, key)

    def decode_result(self, value: Any) -> Any:  # noqa: ANN401
        if value is None:
            return None
        if isinstance(value, str):
            # asyncpg may return BYTEA as a memoryview or bytes; if a str
            # arrives (e.g. from a text-cast), try encoding it.
            value = value.encode("latin-1")
        if not isinstance(value, (bytes, bytearray, memoryview)):
            raise FerrumCodecError(
                f"EncryptedStringCodec expected bytes from BYTEA, got {type(value).__name__}.",
                codec_kind="encrypted_string",
                key_id=self.codec_meta.key_id,
            )
        data = bytes(value)
        key = self._key_provider.get_key(self.codec_meta.key_id or "")
        plaintext = _decrypt(data, key)
        return plaintext.decode("utf-8")

    def redact(self, value: Any) -> str:  # noqa: ANN401
        return f"[REDACTED:encrypted_string:key_id={self.codec_meta.key_id}]"


class EncryptedJSONCodec:
    """Encrypted JSON codec with key-provider injection.

    Serializes a JSON-serializable value (dict, list, etc.) to JSON,
    encrypts the JSON bytes, and stores as BYTEA. On decode, decrypts
    and parses the JSON back to the original structure.
    """

    def __init__(self, codec_meta: CodecMeta, *, key_provider: KeyProvider) -> None:
        if codec_meta.key_id is None:
            raise FerrumCodecError(
                "EncryptedJSONCodec requires codec_meta.key_id.",
                codec_kind="encrypted_json",
            )
        self.codec_meta = codec_meta
        self._key_provider = key_provider

    def encode_bind(self, value: Any) -> Any:  # noqa: ANN401
        if value is None:
            return None
        plaintext = json.dumps(value).encode("utf-8")
        key = self._key_provider.get_key(self.codec_meta.key_id or "")
        return _encrypt(plaintext, key)

    def decode_result(self, value: Any) -> Any:  # noqa: ANN401
        if value is None:
            return None
        if isinstance(value, str):
            value = value.encode("latin-1")
        if not isinstance(value, (bytes, bytearray, memoryview)):
            raise FerrumCodecError(
                f"EncryptedJSONCodec expected bytes from BYTEA, got {type(value).__name__}.",
                codec_kind="encrypted_json",
                key_id=self.codec_meta.key_id,
            )
        data = bytes(value)
        key = self._key_provider.get_key(self.codec_meta.key_id or "")
        plaintext = _decrypt(data, key)
        return json.loads(plaintext.decode("utf-8"))

    def redact(self, value: Any) -> str:  # noqa: ANN401
        return f"[REDACTED:encrypted_json:key_id={self.codec_meta.key_id}]"


class InetCodec:
    """Codec for PostgreSQL ``inet`` columns.

    Converts between Python :class:`ipaddress.IPv4Address` /
    :class:`ipaddress.IPv6Address` and PostgreSQL inet text representation.
    If the value is already a string, it passes through (asyncpg handles
    inet natively).
    """

    def __init__(self, codec_meta: CodecMeta) -> None:
        self.codec_meta = codec_meta

    def encode_bind(self, value: Any) -> Any:  # noqa: ANN401
        if value is None:
            return None
        if isinstance(value, str):
            return str(ipaddress.ip_address(value))
        if isinstance(value, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
            return str(value)
        raise FerrumCodecError(
            f"InetCodec expected str or ip address, got {type(value).__name__}.",
            codec_kind="inet",
        )

    def decode_result(self, value: Any) -> Any:  # noqa: ANN401
        if value is None:
            return None
        if isinstance(value, str):
            return ipaddress.ip_address(value)
        if isinstance(value, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
            return value
        raise FerrumCodecError(
            f"InetCodec expected str or ip address from DB, got {type(value).__name__}.",
            codec_kind="inet",
        )

    def redact(self, value: Any) -> str:  # noqa: ANN401
        if self.codec_meta.pii:
            return "[REDACTED:pii:inet]"
        return f"[inet:{value}]" if value is not None else "[inet:null]"


class VectorCodec:
    """Codec for pgvector ``VECTOR(n)`` columns.

    Encodes a list of floats to pgvector text representation and decodes
    the text back to a list of floats. The dimensions are validated
    against ``codec_meta`` (which mirrors ``FieldMeta.vector_dimensions``).
    """

    def __init__(self, codec_meta: CodecMeta, *, dimensions: int | None = None) -> None:
        self.codec_meta = codec_meta
        self._dimensions = dimensions

    def encode_bind(self, value: Any) -> Any:  # noqa: ANN401
        if value is None:
            return None
        if isinstance(value, str):
            return value
        if not isinstance(value, (list, tuple)):
            raise FerrumCodecError(
                f"VectorCodec expected a list, got {type(value).__name__}.",
                codec_kind="vector",
            )
        if self._dimensions is not None and len(value) != self._dimensions:
            raise FerrumCodecError(
                f"VectorCodec expected {self._dimensions} dimensions, got {len(value)}.",
                codec_kind="vector",
            )
        return "[" + ",".join(str(v) for v in value) + "]"

    def decode_result(self, value: Any) -> Any:  # noqa: ANN401
        if value is None:
            return None
        if isinstance(value, str):
            inner = value.strip("[]")
            if not inner:
                return []
            return [float(p) for p in inner.split(",")]
        if isinstance(value, (list, tuple)):
            return list(value)
        raise FerrumCodecError(
            f"VectorCodec expected a str or list from DB, got {type(value).__name__}.",
            codec_kind="vector",
        )

    def redact(self, value: Any) -> str:  # noqa: ANN401
        return f"[vector:dims={self._dimensions}]"


# Codec kind → field_type override mapping. When a codec is specified via
# Field(), the field_type is set from this mapping instead of the
# annotation-derived type. This ensures DDL and SQL compilation use the
# correct PostgreSQL type for codec-backed columns.
_CODEC_FIELD_TYPE_OVERRIDES: dict[str, str] = {
    "encrypted_string": "bytes",
    "encrypted_json": "bytes",
    "nested_model": "json",
    "nested_list": "json",
    "citext": "citext",
    "inet": "inet",
    "bytea": "bytes",
    "enum": "enum",
    "vector": "vector",
    "domain": "domain",
    "array": "array_text",
    "passthrough": "text",
}


# ---------------------------------------------------------------------------
# Default codec factory registration
#
# Each factory constructs a FieldCodec from a CodecMeta plus optional
# runtime context (key_provider). The factories never close over key
# material — they receive the key provider as a parameter at query time.
# ---------------------------------------------------------------------------


def _make_passthrough_factory(
    codec_cls: type[PassthroughCodec],
) -> Callable[..., FieldCodec]:
    def factory(
        meta: CodecMeta,
        *,
        key_provider: KeyProvider | None = None,
    ) -> FieldCodec:
        return codec_cls(meta)

    return factory


def _make_simple_factory(
    codec_cls: type[InetCodec | VectorCodec],
) -> Callable[..., FieldCodec]:
    """Factory maker for codecs that take only codec_meta (no key provider)."""

    def factory(
        meta: CodecMeta,
        *,
        key_provider: KeyProvider | None = None,
    ) -> FieldCodec:
        return codec_cls(meta)

    return factory


def _make_encrypted_string_factory() -> Callable[..., FieldCodec]:
    def factory(
        meta: CodecMeta,
        *,
        key_provider: KeyProvider | None = None,
    ) -> FieldCodec:
        if key_provider is None:
            raise FerrumCodecError(
                "EncryptedStringCodec requires a key_provider at query time.",
                codec_kind="encrypted_string",
                key_id=meta.key_id,
            )
        return EncryptedStringCodec(meta, key_provider=key_provider)

    return factory


def _make_encrypted_json_factory() -> Callable[..., FieldCodec]:
    def factory(
        meta: CodecMeta,
        *,
        key_provider: KeyProvider | None = None,
    ) -> FieldCodec:
        if key_provider is None:
            raise FerrumCodecError(
                "EncryptedJSONCodec requires a key_provider at query time.",
                codec_kind="encrypted_json",
                key_id=meta.key_id,
            )
        return EncryptedJSONCodec(meta, key_provider=key_provider)

    return factory


def _make_nested_model_factory() -> Callable[..., FieldCodec]:
    def factory(
        meta: CodecMeta,
        *,
        key_provider: KeyProvider | None = None,
    ) -> FieldCodec:
        if meta.model_class_name is None:
            raise FerrumCodecError(
                "NestedModelCodec requires codec_meta.model_class_name.",
                codec_kind="nested_model",
            )
        from ferrum.registry import get_model

        model_cls = get_model(meta.model_class_name)
        return NestedModelCodec(meta, model_cls=model_cls)

    return factory


def _make_nested_list_factory() -> Callable[..., FieldCodec]:
    def factory(
        meta: CodecMeta,
        *,
        key_provider: KeyProvider | None = None,
    ) -> FieldCodec:
        if meta.model_class_name is None:
            raise FerrumCodecError(
                "NestedListModelCodec requires codec_meta.model_class_name.",
                codec_kind="nested_list",
            )
        from ferrum.registry import get_model

        model_cls = get_model(meta.model_class_name)
        return NestedListModelCodec(meta, model_cls=model_cls)

    return factory


def _register_default_codecs() -> None:
    """Register default codec factories (idempotent, module-level)."""
    from ferrum.registry import register_codec_factory

    register_codec_factory("passthrough", _make_passthrough_factory(PassthroughCodec))
    register_codec_factory("bytea", _make_passthrough_factory(PassthroughCodec))
    register_codec_factory("citext", _make_passthrough_factory(PassthroughCodec))
    register_codec_factory("enum", _make_passthrough_factory(PassthroughCodec))
    register_codec_factory("domain", _make_passthrough_factory(PassthroughCodec))
    register_codec_factory("array", _make_passthrough_factory(PassthroughCodec))
    register_codec_factory("encrypted_string", _make_encrypted_string_factory())
    register_codec_factory("encrypted_json", _make_encrypted_json_factory())
    register_codec_factory("nested_model", _make_nested_model_factory())
    register_codec_factory("nested_list", _make_nested_list_factory())
    register_codec_factory("inet", _make_simple_factory(InetCodec))
    register_codec_factory("vector", _make_simple_factory(VectorCodec))


_register_default_codecs()


# ---------------------------------------------------------------------------
# Relationship descriptor classes (class-level, analogous to ClassVar[_Manager])
# ---------------------------------------------------------------------------


class ForeignKey:
    """Declare a many-to-one relationship.

    Attach as a ``ClassVar`` so Pydantic ignores it::

        class Post(Model):
            author_id: int
            author: ClassVar[ForeignKey] = ForeignKey(to="User", on_delete="CASCADE")

    The backing FK column defaults to ``{field_name}_id``.  Override with
    ``db_column`` if your schema names it differently.  The column must be
    declared as a typed field (``author_id: int``) for full Pydantic validation;
    ``_build_metadata`` will auto-add a virtual ``FieldMeta`` for DDL purposes
    only when the column is absent from ``model_fields``.

    ``on_delete`` is validated against ``_ON_DELETE_ALLOWLIST`` at class-definition
    time and interpolated into DDL only via the orchestrator allowlist check.
    """

    def __init__(
        self,
        to: str,
        *,
        db_column: str | None = None,
        on_delete: str = "CASCADE",
        related_name: str | None = None,
    ) -> None:
        self.to = to
        self.db_column = db_column
        self.on_delete = on_delete
        self.related_name = related_name

    def __class_getitem__(cls, item: Any) -> type[ForeignKey]:  # noqa: ANN401
        """Support ``ForeignKey["ModelName"]`` annotation syntax."""
        return cls

    def __repr__(self) -> str:
        return f"ForeignKey(to={self.to!r}, on_delete={self.on_delete!r})"


class OneToOne:
    """Declare a one-to-one relationship (unique FK).

    Usage mirrors :class:`ForeignKey`::

        class Profile(Model):
            user_id: int
            user: ClassVar[OneToOne] = OneToOne(to="User", on_delete="CASCADE")
    """

    def __init__(
        self,
        to: str,
        *,
        db_column: str | None = None,
        on_delete: str = "CASCADE",
        related_name: str | None = None,
    ) -> None:
        self.to = to
        self.db_column = db_column
        self.on_delete = on_delete
        self.related_name = related_name

    def __class_getitem__(cls, item: Any) -> type[OneToOne]:  # noqa: ANN401
        """Support ``OneToOne["ModelName"]`` annotation syntax."""
        return cls

    def __repr__(self) -> str:
        return f"OneToOne(to={self.to!r}, on_delete={self.on_delete!r})"


class ManyToMany:
    """Declare a many-to-many relationship (generates a join table).

    Usage::

        class Post(Model):
            tags: ClassVar[ManyToMany] = ManyToMany(to="Tag")

    The join table name defaults to the two table names sorted alphabetically
    and joined with ``_`` (e.g. ``post_tag``).  Override with ``through``.

    ``through_fields`` is an optional ``(from_col, to_col)`` tuple that names the
    FK columns on the join table when they differ from ``{table}_id`` defaults.
    """

    def __init__(
        self,
        to: str,
        *,
        through: str | None = None,
        through_fields: tuple[str, str] | None = None,
        related_name: str | None = None,
    ) -> None:
        self.to = to
        self.through = through
        self.through_fields = through_fields
        self.related_name = related_name

    def __class_getitem__(cls, item: Any) -> type[ManyToMany]:  # noqa: ANN401
        """Support ``ManyToMany["ModelName"]`` annotation syntax."""
        return cls

    def __repr__(self) -> str:
        return f"ManyToMany(to={self.to!r}, through={self.through!r})"


# ---------------------------------------------------------------------------
# ModelConfig factory
# ---------------------------------------------------------------------------


def ModelConfig(  # noqa: N802
    *,
    table: str | None = None,
    **kwargs: Any,  # noqa: ANN401
) -> ConfigDict:
    """Ferrum model configuration factory.

    Extends ``pydantic.ConfigDict`` with Ferrum-specific options. The ``table``
    parameter sets the database table name; it defaults to the snake_case class
    name when omitted.

    Example::

        class User(ferrum.Model):
            model_config = ferrum.ModelConfig(table="users")
            id: int
            email: str
    """
    if table is not None:
        # Piggyback on json_schema_extra (a valid Pydantic ConfigDict key) to
        # carry the Ferrum-private table name without triggering unknown-key errors.
        existing_jse = kwargs.pop("json_schema_extra", None) or {}
        if isinstance(existing_jse, dict):
            kwargs["json_schema_extra"] = {"__ferrum_table__": table, **existing_jse}
    return ConfigDict(**kwargs)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Field factory
# ---------------------------------------------------------------------------

_DB_DEFAULT_STRINGS: frozenset[str] = frozenset(
    {
        "NULL",
        "TRUE",
        "FALSE",
        "NOW()",
        "CURRENT_TIMESTAMP",
        "CURRENT_DATE",
        "CURRENT_TIME",
        "GEN_RANDOM_UUID()",
        "UUIDV7()",
        "0",
        "1",
        "''",
    }
)


def _is_db_default_expression(value: str) -> bool:
    """Return True when *value* is a DB-side default expression, not a Python literal."""
    if value.upper() in _DB_DEFAULT_STRINGS:
        return True
    return "(" in value and ")" in value


def _normalize_db_default(value: str) -> str:
    """Normalize a DB default token to its canonical uppercase form.

    Maps well-known SQL function tokens to their uppercase canonical form
    (``now()`` → ``NOW()``, ``gen_random_uuid()`` → ``GEN_RANDOM_UUID()``) so
    that the default allowlist check and equality comparisons in ``compute_plan``
    are case-insensitive.  Unknown expressions pass through unchanged.
    """
    upper = value.upper()
    if upper in _DB_DEFAULT_STRINGS:
        return upper
    return value


def Field(  # noqa: N802
    *,
    max_length: int | None = None,
    max_digits: int | None = None,
    decimal_places: int | None = None,
    db_column: str | None = None,
    unique: bool = False,
    db_index: bool = False,
    default: Any = ...,  # noqa: ANN401
    db_default: str | None = None,
    nullable: bool | None = None,
    primary_key: bool = False,
    uuid_generate: Literal["v4", "v7"] | None = None,
    vector_dimensions: int | None = None,
    fts_config: str | None = None,
    fts_source_columns: list[str] | None = None,
    jsonb_list: bool = False,
    generated: bool = False,
    read_only: bool = False,
    codec_kind: str | None = None,
    codec_pii: bool = False,
    codec_key_id: str | None = None,
    codec_domain: str | None = None,
    codec_model: type | str | None = None,
    codec_element_type: str | None = None,
    **kwargs: Any,  # noqa: ANN401
) -> Any:  # noqa: ANN401
    """Ferrum field descriptor with column-level constraints.

    Wraps ``pydantic.Field`` and stores Ferrum-specific extras in
    ``json_schema_extra`` under the ``"__ferrum__"`` key.  ``_build_metadata``
    reads those extras at class-definition time.

    ``default`` handling:
    - ``...`` (no default): field is required; Pydantic treats it as required.
    - ``str`` DB expression (e.g. ``"NOW()"``, ``"''"``): stored as ``db_default``;
      Python-side default is ``None``.
    - Plain ``str`` literals (including ``""``): Python-side default; migrations may
      emit a matching SQL default for ``NOT NULL`` text columns.
    - Any other value: passed to Pydantic as ``default``.

    ``db_default`` (explicit):
    - Accepts a SQL default expression string directly (e.g. ``"now()"``, ``"NOW()"``).
    - Normalized to canonical uppercase form before storage.
    - Takes precedence over the ``default=`` string-expression path.

    ``nullable``:
    - When provided, overrides the annotation-derived nullability in
      ``_build_metadata``.  Use to mark a ``T | None``-annotated field as
      ``NOT NULL`` (e.g. ``Field(nullable=False)``).

    ``jsonb_list`` explicitly stores a ``list[T]`` annotation as JSONB instead
    of PostgreSQL ARRAY. ``generated`` and ``read_only`` fields remain available
    to SELECT/hydration metadata but are excluded from write metadata.
    """
    ferrum_extras: dict[str, Any] = {
        "max_length": max_length,
        "max_digits": max_digits,
        "decimal_places": decimal_places,
        "db_column": db_column,
        "unique": unique,
        "db_index": db_index,
        "primary_key": primary_key,
        "vector_dimensions": vector_dimensions,
        "fts_config": fts_config,
        "fts_source_columns": fts_source_columns,
        "jsonb_list": jsonb_list,
        "generated": generated,
        "read_only": read_only,
        "codec_kind": codec_kind,
        "codec_pii": codec_pii,
        "codec_key_id": codec_key_id,
        "codec_domain": codec_domain,
        "codec_model": codec_model.__name__ if isinstance(codec_model, type) else codec_model,
        "codec_element_type": codec_element_type,
    }

    # Store explicit nullable override for _build_metadata to consume.
    if nullable is not None:
        ferrum_extras["nullable"] = nullable

    if uuid_generate == "v4":
        ferrum_extras["db_default"] = "GEN_RANDOM_UUID()"
    elif uuid_generate == "v7":
        ferrum_extras["db_default"] = "UUIDV7()"

    if isinstance(default, str) and _is_db_default_expression(default):
        ferrum_extras["db_default"] = _normalize_db_default(default)
        kwargs["default"] = None
    elif default is not ...:
        kwargs["default"] = default

    # Explicit db_default= param takes highest priority over all other paths.
    if db_default is not None:
        ferrum_extras["db_default"] = _normalize_db_default(db_default)

    if max_length is not None:
        kwargs["max_length"] = max_length

    kwargs["json_schema_extra"] = {"__ferrum__": ferrum_extras}
    return _PydanticField(**kwargs)


# ---------------------------------------------------------------------------
# Metadata builder (definition-time only; no I/O)
# ---------------------------------------------------------------------------


def _resolve_list_field_type(base_type: Any, *, model_name: str, field_name: str) -> str:  # noqa: ANN401
    """Return the Ferrum field type for a parameterized ``list[T]`` annotation."""
    args = get_args(base_type)
    if not args:
        return "array_text"
    elem = args[0]
    if get_origin(elem) is Union or (
        hasattr(_types, "UnionType") and isinstance(elem, _types.UnionType)
    ):
        raise TypeError(
            f"Model {model_name!r} field {field_name!r}: PostgreSQL ARRAY elements "
            "must be non-nullable scalar values."
        )
    if elem is str:
        return "array_text"
    if elem is int:
        return "array_int"
    if elem is UUID:
        return "array_uuid"
    if elem is float:
        return "array_float"
    raise TypeError(
        f"Model {model_name!r} field {field_name!r}: unsupported PostgreSQL ARRAY "
        f"element type {elem!r}. Supported types: str, int, UUID, float. "
        "Use Field(jsonb_list=True) for list-shaped JSONB."
    )


def _build_metadata(cls: type[_PydanticBaseModel]) -> ModelMetadata:
    """Derive ``ModelMetadata`` from a Pydantic v2 model's ``model_fields``.

    Called once per concrete model class during ``__init_subclass__``.
    The result is frozen and registered on the class.
    """
    # --- table name resolution (priority: ModelConfig > inner Meta > snake_case) ---
    table_name = _to_snake_case(cls.__name__)

    jse = cls.model_config.get("json_schema_extra") or {}
    if isinstance(jse, dict):
        ferrum_table = jse.get("__ferrum_table__")
        if ferrum_table:
            table_name = str(ferrum_table)

    meta_cls = cls.__dict__.get("Meta")
    if meta_cls is not None:
        meta_table = getattr(meta_cls, "table", None)
        if meta_table:
            table_name = str(meta_table)

    # --- field list derivation ---
    fields: list[FieldMeta] = []
    pk_indices: list[int] = []

    for idx, (name, field_info) in enumerate(cls.model_fields.items()):
        annotation = field_info.annotation
        base_type, nullable = _unwrap_optional(annotation)
        ferrum_extras: dict[str, Any] = {}
        if get_origin(base_type) is Annotated:
            ann_args = get_args(base_type)
            base_type = ann_args[0]
            base_type, ann_nullable = _unwrap_optional(base_type)
            nullable = nullable or ann_nullable
            for meta in ann_args[1:]:
                jse = getattr(meta, "json_schema_extra", None)
                if isinstance(jse, dict) and "__ferrum__" in jse:
                    ferrum_extras = jse["__ferrum__"]
                    break

        # Extract Ferrum field extras from Annotated metadata or FieldInfo.json_schema_extra.
        # Annotated[T, Field(...)] path: Pydantic stores the FieldInfo items in metadata.
        if not ferrum_extras:
            for meta in getattr(field_info, "metadata", []):
                jse = getattr(meta, "json_schema_extra", None)
                if isinstance(jse, dict) and "__ferrum__" in jse:
                    ferrum_extras = jse["__ferrum__"]
                    break
        # Bare default-value path: `field: T = Field(...)` stores extras directly.
        finfo_jse = field_info.json_schema_extra or {}
        if isinstance(finfo_jse, dict) and "__ferrum__" in finfo_jse:
            ferrum_extras = finfo_jse["__ferrum__"]

        # Honor explicit nullable= override from Field() — wins over annotation-derived value.
        nullable_override = ferrum_extras.get("nullable")
        if nullable_override is not None:
            nullable = bool(nullable_override)

        is_pk = bool(ferrum_extras.get("primary_key", False))

        # Implicit PK: first int field named "id" when no explicit PK is declared.
        if not is_pk and not pk_indices and name == "id" and base_type is int:
            is_pk = True

        if is_pk:
            pk_indices.append(idx)

        # --- field type resolution ---
        enum_values: tuple[str, ...] | None = None
        origin = get_origin(base_type)

        jsonb_list = bool(ferrum_extras.get("jsonb_list", False))
        generated = bool(ferrum_extras.get("generated", False))
        read_only = bool(ferrum_extras.get("read_only", False))
        is_list_annotation = origin is list or base_type is list
        if jsonb_list and not is_list_annotation:
            raise TypeError(
                f"Model {cls.__name__!r} field {name!r}: Field(jsonb_list=True) "
                "requires a list annotation."
            )
        if origin is Literal:
            # Literal["a", "b"] → enum field type with TEXT + CHECK constraint
            db_type = "enum"
            enum_values = tuple(str(v) for v in get_args(base_type))
        elif jsonb_list:
            db_type = "json"
        elif origin is list:
            db_type = _resolve_list_field_type(
                base_type,
                model_name=cls.__name__,
                field_name=name,
            )
        elif base_type is list:
            db_type = "array_text"
        else:
            db_type = _SUPPORTED_TYPES.get(base_type, "text")  # type: ignore[arg-type]

        # Integer PK columns use big_int semantics (DATA_MODELING.md §3.4).
        if is_pk and db_type == "int":
            db_type = "big_int"

        column_name = ferrum_extras.get("db_column") or name

        db_default = ferrum_extras.get("db_default")
        if db_default is not None and isinstance(db_default, str):
            db_default = _normalize_db_default(db_default)
        if is_pk and db_type == "uuid" and db_default is None:
            db_default = "GEN_RANDOM_UUID()"

        python_default: Any | None = None
        if field_info.default is not ...:
            python_default = field_info.default

        vector_dimensions = ferrum_extras.get("vector_dimensions")
        fts_config = ferrum_extras.get("fts_config")
        if fts_config is not None:
            cfg = str(fts_config)
            if not cfg.replace("_", "").isalnum():
                raise ValueError(
                    f"Model {cls.__name__!r} field {name!r}: invalid fts_config {cfg!r}."
                )
        raw_fts_source = ferrum_extras.get("fts_source_columns")
        fts_source_columns: tuple[str, ...] | None = None
        if raw_fts_source is not None:
            fts_source_columns = tuple(str(c) for c in raw_fts_source)
        if db_type == "vector" and vector_dimensions is None:
            raise ValueError(
                f"Model {cls.__name__!r} field {name!r}: Vector columns require "
                "Field(vector_dimensions=n)."
            )

        # --- codec metadata (W2-A) ---
        codec_kind_val: str | None = ferrum_extras.get("codec_kind")
        codec_meta: CodecMeta | None = None
        domain_name: str | None = ferrum_extras.get("codec_domain")
        if codec_kind_val is not None:
            # Override field_type based on codec kind for correct DDL/SQL.
            override = _CODEC_FIELD_TYPE_OVERRIDES.get(codec_kind_val)
            if override is not None:
                db_type = override
            # Validate encrypted codec has key_id.
            if (
                codec_kind_val in ("encrypted_string", "encrypted_json")
                and ferrum_extras.get("codec_key_id") is None
            ):
                raise ValueError(
                    f"Model {cls.__name__!r} field {name!r}: "
                    f"codec_kind={codec_kind_val!r} requires codec_key_id."
                )
            # Validate domain codec has domain name.
            if codec_kind_val == "domain" and domain_name is None:
                raise ValueError(
                    f"Model {cls.__name__!r} field {name!r}: "
                    "codec_kind='domain' requires codec_domain."
                )
            codec_meta = CodecMeta(
                kind=codec_kind_val,
                pii=bool(ferrum_extras.get("codec_pii", False)),
                key_id=ferrum_extras.get("codec_key_id"),
                model_class_name=ferrum_extras.get("codec_model"),
                domain_name=domain_name,
                element_type=ferrum_extras.get("codec_element_type"),
            )
            # Re-validate vector dimensions for vector codec.
            if codec_kind_val == "vector" and vector_dimensions is None:
                raise ValueError(
                    f"Model {cls.__name__!r} field {name!r}: "
                    "codec_kind='vector' requires vector_dimensions."
                )

        python_type_name: str
        if origin is Literal:
            python_type_name = "Literal"
        elif origin is list:
            python_type_name = "list"
        else:
            python_type_name = getattr(base_type, "__name__", str(base_type))

        fields.append(
            FieldMeta(
                name=name,
                column_name=column_name,
                python_type_name=python_type_name,
                field_type=db_type,
                allowed_operators=_ALLOWED_OPERATORS.get(
                    db_type, ("eq", "is_null", "is_not_null", "ne")
                ),
                nullable=nullable,
                pk=is_pk,
                max_length=ferrum_extras.get("max_length"),
                max_digits=ferrum_extras.get("max_digits"),
                decimal_places=ferrum_extras.get("decimal_places"),
                unique=bool(ferrum_extras.get("unique", False)),
                db_index=bool(ferrum_extras.get("db_index", False)),
                db_default=db_default,
                python_default=python_default,
                vector_dimensions=vector_dimensions,
                enum_values=enum_values,
                fts_config=str(fts_config) if fts_config is not None else None,
                fts_source_columns=fts_source_columns,
                jsonb_list=jsonb_list,
                generated=generated,
                read_only=read_only,
                codec_meta=codec_meta,
                domain_name=domain_name,
            )
        )

    field_column_names: set[str] = {f.column_name for f in fields}
    field_names = {f.name for f in fields}
    indexes: list[IndexMeta] = []
    full_text_indexes: list[FullTextIndexMeta] = []
    if meta_cls is not None:
        raw_indexes = getattr(meta_cls, "indexes", None) or ()
        for raw_index in raw_indexes:
            if isinstance(raw_index, Index):
                index = raw_index
            elif isinstance(raw_index, dict):
                index = Index(**raw_index)
            else:
                raise TypeError(
                    f"Model {cls.__name__!r} Meta.indexes entries must be Index instances."
                )
            if not index.fields:
                raise ValueError(f"Model {cls.__name__!r} Index requires at least one field.")
            for index_field in index.fields:
                if index_field not in field_names:
                    raise ValueError(
                        f"Model {cls.__name__!r} Index references unknown field {index_field!r}."
                    )
            cols_joined = "_".join(index.fields)
            index_name = index.name or f"idx_{table_name}_{cols_joined}"
            indexes.append(
                IndexMeta(
                    name=index_name,
                    fields=index.fields,
                    unique=index.unique,
                    using=index.using,
                    where=index.where,
                )
            )

        raw_fts_indexes = getattr(meta_cls, "full_text_indexes", None) or ()
        for raw_fts in raw_fts_indexes:
            if isinstance(raw_fts, FullTextIndex):
                fts_index = raw_fts
            elif isinstance(raw_fts, dict):
                fts_index = FullTextIndex(**raw_fts)
            else:
                raise TypeError(
                    f"Model {cls.__name__!r} Meta.full_text_indexes entries must be "
                    "FullTextIndex instances."
                )
            if not fts_index.fields:
                raise ValueError(
                    f"Model {cls.__name__!r} FullTextIndex requires at least one field."
                )
            for fts_field in fts_index.fields:
                if fts_field not in field_names:
                    raise ValueError(
                        f"Model {cls.__name__!r} FullTextIndex references unknown field "
                        f"{fts_field!r}."
                    )
            cols_joined = "_".join(fts_index.fields)
            fts_name = fts_index.name or f"fts_{table_name}_{cols_joined}"
            full_text_indexes.append(
                FullTextIndexMeta(
                    name=fts_name,
                    fields=fts_index.fields,
                    config=fts_index.config,
                    sqlite_content_table=fts_index.sqlite_content_table,
                )
            )

    # --- relationship descriptors (ClassVar-style class attributes) ---
    # Scan this class's __dict__ for ForeignKey/OneToOne/ManyToMany (ClassVar
    # keeps them out of model_fields). Then inherit RelationMeta from parent
    # Ferrum models via MRO — parent __dict__ cannot be re-scanned for the
    # original ForeignKey because install_relation_descriptors() replaces it
    # with a forward descriptor after the parent is finalized.
    from ferrum.relations import ReverseRelationMeta, register_reverse

    relations: list[RelationMeta] = []
    seen_rel_names: set[str] = set()

    def _register_fk_oto(
        *,
        attr_name: str,
        kind: str,
        to_model: str,
        db_column: str,
        on_delete: str,
        related_name: str | None,
    ) -> None:
        if db_column not in field_column_names:
            # Auto-add a virtual FieldMeta so the column appears in DDL.
            fields.append(
                FieldMeta(
                    name=db_column,
                    column_name=db_column,
                    python_type_name="int",
                    field_type="int",
                    allowed_operators=_ALLOWED_OPERATORS["int"],
                    nullable=False,
                    pk=False,
                )
            )
            field_column_names.add(db_column)
        relations.append(
            RelationMeta(
                field_name=attr_name,
                kind=kind,
                to_model=to_model,
                db_column=db_column,
                on_delete=on_delete,
                related_name=related_name,
            )
        )
        accessor = related_name or f"{_to_snake_case(cls.__name__)}_set"
        register_reverse(
            target_model=to_model,
            meta=ReverseRelationMeta(
                accessor=accessor,
                related_model_name=cls.__name__,
                fk_column=db_column,
                fk_field_name=attr_name,
                kind=kind,
            ),
        )

    for attr_name, attr_value in cls.__dict__.items():
        if isinstance(attr_value, (ForeignKey, OneToOne)):
            seen_rel_names.add(attr_name)
            kind = "fk" if isinstance(attr_value, ForeignKey) else "one_to_one"
            on_delete = attr_value.on_delete.upper()
            if on_delete not in _ON_DELETE_ALLOWLIST:
                raise ValueError(
                    f"Model {cls.__name__!r} field {attr_name!r}: "
                    f"Invalid on_delete {on_delete!r}. "
                    f"Allowed: {sorted(_ON_DELETE_ALLOWLIST)}."
                )
            backing_col = attr_value.db_column or f"{attr_name}_id"
            _register_fk_oto(
                attr_name=attr_name,
                kind=kind,
                to_model=attr_value.to,
                db_column=backing_col,
                on_delete=on_delete,
                related_name=attr_value.related_name,
            )
        elif isinstance(attr_value, ManyToMany):
            seen_rel_names.add(attr_name)
            target_table = _to_snake_case(attr_value.to)
            through_table = attr_value.through or "_".join(sorted([table_name, target_table]))
            relations.append(
                RelationMeta(
                    field_name=attr_name,
                    kind="m2m",
                    to_model=attr_value.to,
                    through_table=through_table,
                )
            )

    for base in cls.__mro__[1:]:
        if base is Model or not issubclass(base, Model):
            continue
        parent_meta = base.__dict__.get("__ferrum_metadata__")
        if parent_meta is None:
            continue
        for rel in parent_meta.relations:
            if rel.field_name in seen_rel_names:
                continue
            seen_rel_names.add(rel.field_name)
            if rel.kind in ("fk", "one_to_one"):
                _register_fk_oto(
                    attr_name=rel.field_name,
                    kind=rel.kind,
                    to_model=rel.to_model,
                    db_column=rel.db_column or f"{rel.field_name}_id",
                    on_delete=rel.on_delete or "CASCADE",
                    related_name=rel.related_name,
                )
            elif rel.kind == "m2m":
                relations.append(rel)

    # Default to field 0 as the single PK when nothing was marked as primary_key=True
    # and no implicit "id" int field was found.
    if not pk_indices and fields:
        pk_indices = [0]

    fts_field_names: set[str] = set()
    for idx in full_text_indexes:
        fts_field_names.update(idx.fields)
    if fts_field_names:
        fts_ops = _ALLOWED_OPERATORS["tsvector"]
        fields = [
            dataclasses.replace(f, allowed_operators=fts_ops)
            if f.name in fts_field_names and f.field_type == "text"
            else f
            for f in fields
        ]

    return ModelMetadata(
        table_name=table_name,
        model_name=cls.__name__,
        fields=tuple(fields),
        indexes=tuple(indexes),
        full_text_indexes=tuple(full_text_indexes),
        pk_fields=tuple(pk_indices),
        relations=tuple(relations),
    )


# ---------------------------------------------------------------------------
# Manager descriptor
# ---------------------------------------------------------------------------

if TYPE_CHECKING:
    from ferrum.queryset import QuerySet

# Bound to the accessing model type via ``owner`` so ``User.objects`` infers
# ``QuerySet[User]`` in IDEs / type checkers (PEP 561 descriptor idiom).
_M = TypeVar("_M", bound="Model")


class _Manager:
    """Descriptor that vends a fresh ``QuerySet`` bound to the model class.

    Accessible via the class only (e.g. ``User.objects``); instance access raises
    ``AttributeError`` to avoid confusion with persisted row attributes.

    The import of ``QuerySet`` is deferred inside ``__get__`` to avoid a
    module-level circular dependency between ``ferrum.models`` and
    ``ferrum.queryset`` (models is the lower layer; queryset depends on it).
    """

    @overload
    def __get__(self, obj: None, owner: type[_M]) -> QuerySet[_M]: ...
    @overload
    def __get__(self, obj: Model, owner: type[_M]) -> NoReturn: ...

    def __get__(self, obj: object, owner: type | None = None) -> Any:
        if obj is not None:
            raise AttributeError(
                "'objects' is a class-level manager and cannot be accessed on a model instance."
            )
        if owner is None:
            raise AttributeError("'objects' was accessed without a class.")
        from ferrum.queryset import QuerySet  # deferred — see class docstring

        return QuerySet(cast("type[Any]", owner))


# ---------------------------------------------------------------------------
# Model base class
# ---------------------------------------------------------------------------


class Model(_PydanticBaseModel):
    """Base class for all Ferrum models.

    Subclass this to define a persisted entity::

        class User(ferrum.Model):
            model_config = ferrum.ModelConfig(table="users")

            id: int
            email: str
            active: bool = True

    ``Model.get_metadata()`` returns the immutable ``ModelMetadata`` built once
    at class-definition time and shared read-only across all async tasks.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(
        # Pydantic v2: validate on assignment for safety; the ORM may relax this
        # on internal hydration paths (construct-without-revalidate, ADR-003).
        validate_assignment=True,
        # Forbid extra fields by default — schema drift is surfaced early.
        extra="forbid",
    )

    __ferrum_table__: ClassVar[str] = ""
    __ferrum_metadata__: ClassVar[ModelMetadata | None] = None
    objects: ClassVar[_Manager] = _Manager()

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:  # noqa: ANN401
        """Build immutable ``ModelMetadata`` after Pydantic has finalized ``model_fields``.

        Pydantic v2 calls this hook from ``ModelMetaclass.__new__`` **after**
        ``model_fields`` is populated — unlike ``__init_subclass__``, which runs
        before the field descriptors are ready.
        """
        super().__pydantic_init_subclass__(**kwargs)
        if cls.model_fields:
            metadata = _build_metadata(cls)
            cls.__ferrum_table__ = metadata.table_name
            cls.__ferrum_metadata__ = metadata
            from ferrum.registry import register_model
            from ferrum.relations import install_relation_descriptors

            register_model(cls)
            install_relation_descriptors(cls)

    @classmethod
    def get_metadata(cls) -> ModelMetadata:
        """Return the immutable ``ModelMetadata``, built once at class definition.

        Raises:
            AttributeError: if the model has no fields (misconfigured subclass).
        """
        if cls.__ferrum_metadata__ is None:
            raise AttributeError(
                f"Model {cls.__name__!r} has no metadata. Ensure it defines at least one field."
            )
        return cls.__ferrum_metadata__

    def __getattribute__(self, name: str) -> Any:  # noqa: ANN401
        if name.startswith("_") or name in {
            "model_fields",
            "model_config",
            "model_computed_fields",
            "objects",
            "get_metadata",
            "model_construct",
            "model_dump",
            "model_copy",
        }:
            return super().__getattribute__(name)
        try:
            deferred = object.__getattribute__(self, "__ferrum_deferred__")
        except AttributeError:
            deferred = None
        if deferred and name in deferred:
            from ferrum.errors import FerrumDeferredFieldError

            raise FerrumDeferredFieldError(
                f"Field {name!r} was deferred and is not loaded. "
                "Use only()/defer() carefully or fetch the field explicitly. [FERR-Q406]"
            )
        return super().__getattribute__(name)
