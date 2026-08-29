"""Model registry for resolving relationship ``to=`` targets at runtime.

Also hosts the codec factory registry (W2-A): codec kinds map to factory
callables that construct a :class:`~ferrum.models.FieldCodec` from a
:class:`~ferrum.models.CodecMeta` plus optional runtime context (key
provider). The registry itself never stores key material or codec
instances — only factory functions.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ferrum.models import FieldCodec, Model

_REGISTRY: dict[str, type[Model]] = {}

# Codec factory registry: maps codec kind → factory callable.
# The factory signature is:
#   factory(codec_meta: CodecMeta, *, key_provider: KeyProvider | None = None) -> FieldCodec
# Factories are registered at import time by the models module. The registry
# never stores key material, codec instances, or PII — only factory callables.
_CODEC_FACTORIES: dict[str, Callable[..., Any]] = {}


def register_model(model_cls: type[Model]) -> None:
    """Record a model class by name for relationship and migration resolution.

    Registration happens at model class-definition time after Pydantic has built
    ``model_fields`` and Ferrum has produced immutable metadata. Re-registering
    the same name replaces the entry, which keeps test modules reload-friendly.
    """
    _REGISTRY[model_cls.__name__] = model_cls


def get_model(name: str) -> type[Model]:
    """Return a registered model class by name.

    Raises a compile error when the target was never imported, which keeps
    relationship mistakes visible before SQL generation.
    """
    from ferrum.errors import FerrumCompileError

    try:
        return _REGISTRY[name]
    except KeyError as exc:
        raise FerrumCompileError(
            f"Unknown model {name!r}. Ensure the related model is imported before use.",
            model=name,
        ) from exc


def all_models() -> dict[str, type[Model]]:
    """Return a snapshot of registered models for discovery callers."""
    return dict(_REGISTRY)


def clear_registry_for_tests() -> None:
    """Test helper — not public API."""
    _REGISTRY.clear()


def model_for_table(table_name: str) -> type[Model] | None:
    """Return the registered model mapped to a database table, if any."""
    for cls in _REGISTRY.values():
        if cls.__ferrum_table__ == table_name:
            return cls
    return None


# ---------------------------------------------------------------------------
# Codec factory registry (W2-A)
# ---------------------------------------------------------------------------


def register_codec_factory(
    kind: str,
    factory: Callable[..., FieldCodec],
) -> None:
    """Register a codec factory for a codec kind.

    The factory is called at query time (by W2-B) with a
    :class:`~ferrum.models.CodecMeta` and optional ``key_provider`` to
    produce a :class:`~ferrum.models.FieldCodec` instance. The factory
    must not close over key material — it receives the key provider as
    a parameter.
    """
    _CODEC_FACTORIES[kind] = factory


def get_codec_factory(kind: str) -> Callable[..., FieldCodec]:
    """Return the registered codec factory for a kind.

    Raises a compile error when no factory is registered for the kind.
    """
    from ferrum.errors import FerrumCompileError

    try:
        return _CODEC_FACTORIES[kind]  # type: ignore[no-any-return]
    except KeyError as exc:
        raise FerrumCompileError(
            f"Unknown codec kind {kind!r}. Register a factory with "
            "ferrum.registry.register_codec_factory().",
        ) from exc


def all_codec_kinds() -> tuple[str, ...]:
    """Return a snapshot of registered codec kinds."""
    return tuple(_CODEC_FACTORIES.keys())


def clear_codec_registry_for_tests() -> None:
    """Test helper — clear all registered codec factories."""
    _CODEC_FACTORIES.clear()
