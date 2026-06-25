"""Model registry for resolving relationship ``to=`` targets at runtime."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ferrum.models import Model

_REGISTRY: dict[str, type[Model]] = {}


def register_model(model_cls: type[Model]) -> None:
    """Record a model class by name for relationship resolution."""
    _REGISTRY[model_cls.__name__] = model_cls


def get_model(name: str) -> type[Model]:
    """Return a registered model class by name."""
    from ferrum.errors import FerrumCompileError

    try:
        return _REGISTRY[name]
    except KeyError as exc:
        raise FerrumCompileError(
            f"Unknown model {name!r}. Ensure the related model is imported before use.",
            model=name,
        ) from exc


def all_models() -> dict[str, type[Model]]:
    return dict(_REGISTRY)


def clear_registry_for_tests() -> None:
    """Test helper — not public API."""
    _REGISTRY.clear()


def model_for_table(table_name: str) -> type[Model] | None:
    for cls in _REGISTRY.values():
        if cls.__ferrum_table__ == table_name:
            return cls
    return None
