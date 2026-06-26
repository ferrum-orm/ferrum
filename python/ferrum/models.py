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
import json
import re
import types as _types
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, ClassVar, Literal, Union, cast, get_args, get_origin
from uuid import UUID

from pydantic import BaseModel as _PydanticBaseModel
from pydantic import ConfigDict
from pydantic import Field as _PydanticField
from pydantic_core import core_schema

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
    "int": ("eq", "gt", "gte", "lt", "lte", "in", "is_null", "ne", "range"),
    "big_int": ("eq", "gt", "gte", "lt", "lte", "in", "is_null", "ne", "range"),
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
        "ne",
    ),
    "bool": ("eq", "is_null", "ne"),
    "float": ("eq", "gt", "gte", "lt", "lte", "in", "is_null", "ne", "range"),
    "decimal": ("eq", "gt", "gte", "lt", "lte", "in", "is_null", "ne", "range"),
    "datetime": ("eq", "gt", "gte", "lt", "lte", "is_null", "ne", "range"),
    "date": ("eq", "gt", "gte", "lt", "lte", "is_null", "ne", "range"),
    "time": ("eq", "gt", "gte", "lt", "lte", "is_null", "ne", "range"),
    "uuid": ("eq", "in", "is_null", "ne"),
    "bytes": ("eq", "in", "is_null", "ne"),
    # JSONB: containment + key existence operators (PostgreSQL @>, ?, ?|)
    "json": ("eq", "is_null", "contains", "has_key", "has_any_keys"),
    "vector": ("is_null",),
    "tsvector": ("match", "is_null"),
    # PostgreSQL array types — array containment and overlap operators
    "array_text": ("eq", "is_null", "contains", "contained_by", "overlap"),
    "array_int": ("eq", "is_null", "contains", "contained_by", "overlap"),
    "array_uuid": ("eq", "is_null", "contains", "contained_by", "overlap"),
    "array_float": ("eq", "is_null", "contains", "contained_by", "overlap"),
    # Enum (TEXT + CHECK constraint) — equality and membership operators
    "enum": ("eq", "is_null", "ne", "in"),
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

    @property
    def sql_type(self) -> str:
        """PostgreSQL DDL type string derived from field_type and constraints."""
        return _field_type_to_sql(self)


@dataclasses.dataclass(frozen=True)
class IndexMeta:
    """Immutable descriptor for a declarative model index (``Meta.indexes``)."""

    name: str
    fields: tuple[str, ...]
    unique: bool = False
    using: str = "btree"
    where: str | None = None


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
    allowed_sort_directions: tuple[str, ...] = ("asc", "desc")
    # pk_fields: all PK field indices in definition order.
    # Defaults to (0,) so the first field is implicitly the PK when not specified.
    pk_fields: tuple[int, ...] = (0,)
    relations: tuple[RelationMeta, ...] = ()

    @property
    def pk_index(self) -> int:
        """Backward-compat alias: index of the *first* PK field."""
        return self.pk_fields[0] if self.pk_fields else 0

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
            field_payloads.append(payload)
        return {
            "model_name": self.model_name,
            "table_name": self.table_name,
            "pk_index": self.pk_index,
            "pk_fields": list(self.pk_fields),
            "fields": field_payloads,
        }

    def to_metadata_json(self) -> str:
        """Serialize to the JSON string expected by ``ferrum._native.compile_query``.

        Produces the ``ModelMetadata`` shape that Rust's serde deserializer
        expects (ADR-002 §ModelMetadata.fields).
        """
        return json.dumps(self.to_metadata_dict())


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
    return "TEXT"


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


def Field(  # noqa: N802
    *,
    max_length: int | None = None,
    max_digits: int | None = None,
    decimal_places: int | None = None,
    db_column: str | None = None,
    unique: bool = False,
    db_index: bool = False,
    default: Any = ...,  # noqa: ANN401
    primary_key: bool = False,
    uuid_generate: Literal["v4", "v7"] | None = None,
    vector_dimensions: int | None = None,
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
    }

    if uuid_generate == "v4":
        ferrum_extras["db_default"] = "gen_random_uuid()"
    elif uuid_generate == "v7":
        ferrum_extras["db_default"] = "uuidv7()"

    if isinstance(default, str) and _is_db_default_expression(default):
        ferrum_extras["db_default"] = default
        kwargs["default"] = None
    elif default is not ...:
        kwargs["default"] = default

    if max_length is not None:
        kwargs["max_length"] = max_length

    kwargs["json_schema_extra"] = {"__ferrum__": ferrum_extras}
    return _PydanticField(**kwargs)


# ---------------------------------------------------------------------------
# Metadata builder (definition-time only; no I/O)
# ---------------------------------------------------------------------------


def _resolve_list_field_type(base_type: Any) -> str:  # noqa: ANN401
    """Return the Ferrum field type for a parameterized ``list[T]`` annotation."""
    args = get_args(base_type)
    if not args:
        return "array_text"
    elem = args[0]
    # Unwrap Optional[T] inside the element type (e.g. list[str | None] is unusual but valid).
    if get_origin(elem) is Union or (
        hasattr(_types, "UnionType") and isinstance(elem, _types.UnionType)
    ):
        non_none = [a for a in get_args(elem) if a is not type(None)]
        elem = non_none[0] if non_none else str
    if elem is str:
        return "array_text"
    if elem is int:
        return "array_int"
    if elem is UUID:
        return "array_uuid"
    if elem is float:
        return "array_float"
    return "array_text"  # safe fallback


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

        # Extract Ferrum field extras from Annotated metadata or FieldInfo.json_schema_extra.
        # Annotated[T, Field(...)] path: Pydantic stores the FieldInfo items in metadata.
        ferrum_extras: dict[str, Any] = {}
        for meta in getattr(field_info, "metadata", []):
            jse = getattr(meta, "json_schema_extra", None)
            if isinstance(jse, dict) and "__ferrum__" in jse:
                ferrum_extras = jse["__ferrum__"]
                break
        # Bare default-value path: `field: T = Field(...)` stores extras directly.
        finfo_jse = field_info.json_schema_extra or {}
        if isinstance(finfo_jse, dict) and "__ferrum__" in finfo_jse:
            ferrum_extras = finfo_jse["__ferrum__"]

        is_pk = bool(ferrum_extras.get("primary_key", False))

        # Implicit PK: first int field named "id" when no explicit PK is declared.
        if not is_pk and not pk_indices and name == "id" and base_type is int:
            is_pk = True

        if is_pk:
            pk_indices.append(idx)

        # --- field type resolution ---
        enum_values: tuple[str, ...] | None = None
        origin = get_origin(base_type)

        if origin is Literal:
            # Literal["a", "b"] → enum field type with TEXT + CHECK constraint
            db_type = "enum"
            enum_values = tuple(str(v) for v in get_args(base_type))
        elif origin is list:
            db_type = _resolve_list_field_type(base_type)
        elif base_type is list:
            db_type = "array_text"
        else:
            db_type = _SUPPORTED_TYPES.get(base_type, "text")  # type: ignore[arg-type]

        # Integer PK columns use big_int semantics (DATA_MODELING.md §3.4).
        if is_pk and db_type == "int":
            db_type = "big_int"

        column_name = ferrum_extras.get("db_column") or name

        db_default = ferrum_extras.get("db_default")
        if is_pk and db_type == "uuid" and db_default is None:
            db_default = "gen_random_uuid()"

        python_default: Any | None = None
        if field_info.default is not ...:
            python_default = field_info.default

        vector_dimensions = ferrum_extras.get("vector_dimensions")
        if db_type == "vector" and vector_dimensions is None:
            raise ValueError(
                f"Model {cls.__name__!r} field {name!r}: Vector columns require "
                "Field(vector_dimensions=n)."
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
                allowed_operators=_ALLOWED_OPERATORS.get(db_type, ("eq", "is_null", "ne")),
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
            )
        )

    field_column_names: set[str] = {f.column_name for f in fields}
    field_names = {f.name for f in fields}
    indexes: list[IndexMeta] = []
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

    # --- relationship descriptors (ClassVar-style class attributes) ---
    # Scan cls.__dict__ directly so we pick up descriptors regardless of whether
    # the attribute appears in model_fields (it shouldn't — ClassVar excludes it).
    relations: list[RelationMeta] = []
    for attr_name, attr_value in cls.__dict__.items():
        if isinstance(attr_value, (ForeignKey, OneToOne)):
            kind = "fk" if isinstance(attr_value, ForeignKey) else "one_to_one"
            on_delete = attr_value.on_delete.upper()
            if on_delete not in _ON_DELETE_ALLOWLIST:
                raise ValueError(
                    f"Model {cls.__name__!r} field {attr_name!r}: "
                    f"Invalid on_delete {on_delete!r}. "
                    f"Allowed: {sorted(_ON_DELETE_ALLOWLIST)}."
                )
            backing_col = attr_value.db_column or f"{attr_name}_id"
            if backing_col not in field_column_names:
                # Auto-add a virtual FieldMeta so the column appears in DDL.
                # Declare the column explicitly as an int field for full Pydantic
                # validation support.
                fields.append(
                    FieldMeta(
                        name=backing_col,
                        column_name=backing_col,
                        python_type_name="int",
                        field_type="int",
                        allowed_operators=_ALLOWED_OPERATORS["int"],
                        nullable=False,
                        pk=False,
                    )
                )
                field_column_names.add(backing_col)
            relations.append(
                RelationMeta(
                    field_name=attr_name,
                    kind=kind,
                    to_model=attr_value.to,
                    db_column=backing_col,
                    on_delete=on_delete,
                    related_name=attr_value.related_name,
                )
            )
            from ferrum.relations import ReverseRelationMeta, register_reverse

            accessor = attr_value.related_name or f"{_to_snake_case(cls.__name__)}_set"
            register_reverse(
                target_model=attr_value.to,
                meta=ReverseRelationMeta(
                    accessor=accessor,
                    related_model_name=cls.__name__,
                    fk_column=backing_col,
                    fk_field_name=attr_name,
                    kind=kind,
                ),
            )
        elif isinstance(attr_value, ManyToMany):
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

    # Default to field 0 as the single PK when nothing was marked as primary_key=True
    # and no implicit "id" int field was found.
    if not pk_indices and fields:
        pk_indices = [0]

    return ModelMetadata(
        table_name=table_name,
        model_name=cls.__name__,
        fields=tuple(fields),
        indexes=tuple(indexes),
        pk_fields=tuple(pk_indices),
        relations=tuple(relations),
    )


# ---------------------------------------------------------------------------
# Manager descriptor
# ---------------------------------------------------------------------------


class _Manager:
    """Descriptor that vends a fresh ``QuerySet`` bound to the model class.

    Accessible via the class only (e.g. ``User.objects``); instance access raises
    ``AttributeError`` to avoid confusion with persisted row attributes.

    The import of ``QuerySet`` is deferred inside ``__get__`` to avoid a
    module-level circular dependency between ``ferrum.models`` and
    ``ferrum.queryset`` (models is the lower layer; queryset depends on it).
    """

    def __get__(self, obj: object, owner: type | None = None) -> Any:  # noqa: ANN401
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
