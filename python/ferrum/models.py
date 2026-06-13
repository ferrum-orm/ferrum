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
from typing import Any, ClassVar, Union, cast, get_args, get_origin
from uuid import UUID

from pydantic import BaseModel as _PydanticBaseModel
from pydantic import ConfigDict

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
}

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
    "json": ("eq", "is_null"),
}


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


@dataclasses.dataclass(frozen=True)
class ModelMetadata:
    """Immutable model metadata built once at class-definition time.

    Serves as the allowlist source for the Rust compiler and the migration
    planner. Never carries connection info, bound values, or row data (DM-7).
    Shared read-only across async tasks — no locks required (ARCHITECTURE §6.3).
    """

    table_name: str
    model_name: str
    fields: tuple[FieldMeta, ...]
    allowed_sort_directions: tuple[str, ...] = ("asc", "desc")
    pk_index: int = 0

    def to_metadata_json(self) -> str:
        """Serialize to the JSON string expected by ``ferrum._native.compile_query``.

        Produces the ``ModelMetadata`` shape that Rust's serde deserializer
        expects (ADR-002 §ModelMetadata.fields). Field ``pk`` is Python-internal
        and is not sent across the boundary.
        """
        return json.dumps(
            {
                "model_name": self.model_name,
                "table_name": self.table_name,
                "pk_index": self.pk_index,
                "fields": [
                    {
                        "name": f.name,
                        "column_name": f.column_name,
                        "field_type": f.field_type,
                        "allowed_operators": list(f.allowed_operators),
                        "nullable": f.nullable,
                    }
                    for f in self.fields
                ],
            }
        )


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
# Metadata builder (definition-time only; no I/O)
# ---------------------------------------------------------------------------


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
    pk_index = 0
    found_pk = False

    for idx, (name, field_info) in enumerate(cls.model_fields.items()):
        annotation = field_info.annotation
        base_type, nullable = _unwrap_optional(annotation)

        # Check for explicit primary_key in FieldInfo.json_schema_extra (ferrum.Field).
        finfo_jse = field_info.json_schema_extra or {}
        is_pk = False
        if isinstance(finfo_jse, dict):
            is_pk = bool(finfo_jse.get("primary_key", False))

        # Implicit PK: first int field named "id" when no explicit PK is declared.
        if not is_pk and not found_pk and name == "id" and base_type is int:
            is_pk = True

        if is_pk and not found_pk:
            found_pk = True
            pk_index = idx

        db_type = _SUPPORTED_TYPES.get(base_type, "text")

        # Integer PK columns use big_int semantics (DATA_MODELING.md §3.4).
        if is_pk and db_type == "int":
            db_type = "big_int"

        fields.append(
            FieldMeta(
                name=name,
                column_name=name,
                python_type_name=getattr(base_type, "__name__", str(base_type)),
                field_type=db_type,
                allowed_operators=_ALLOWED_OPERATORS.get(db_type, ("eq", "is_null", "ne")),
                nullable=nullable,
                pk=is_pk,
            )
        )

    return ModelMetadata(
        table_name=table_name,
        model_name=cls.__name__,
        fields=tuple(fields),
        pk_index=pk_index,
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
