"""Model registry for resolving relationship ``to=`` targets at runtime."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ferrum.models import Model

_REGISTRY: dict[str, type[Model]] = {}


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
