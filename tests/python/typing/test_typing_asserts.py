"""Static-typing assertions for Ferrum's IDE/type-checker support (v0.1.7).

These are checked by ``ty`` (run explicitly against this file, since the default
``[tool.ty.src]`` scope is ``python/ferrum`` only):

    uv run ty check tests/python/typing/test_typing_asserts.py

The ``_check_*`` helpers are intentionally *not* ``test_*`` functions — they are
never executed at runtime (the ``conn`` is a typing-only placeholder). pytest
still imports the module, and ``test_import_smoke`` gives it one runnable test.
"""

from __future__ import annotations

from typing import Any, assert_type, cast

import ferrum
from ferrum.connection import ConnectionLike
from ferrum.queryset import QuerySet


class User(ferrum.Model):
    id: int = 0
    email: str = ""
    is_active: bool = False


# A typing-only placeholder; the ``_check_*`` bodies are never run.
_conn: ConnectionLike = cast(ConnectionLike, None)


def _check_manager_returns_queryset() -> None:
    assert_type(User.objects, "QuerySet[User]")


async def _check_all_returns_list_of_model() -> None:
    assert_type(await User.objects.filter(is_active=True).all(_conn), list[User])


async def _check_first_returns_optional_model() -> None:
    assert_type(await User.objects.first(_conn), "User | None")


async def _check_get_returns_model() -> None:
    assert_type(await User.objects.get(_conn, id=1), User)


async def _check_values_returns_dicts() -> None:
    assert_type(await User.objects.values("id", "email").all(_conn), list[dict[str, Any]])


async def _check_values_list_returns_tuples() -> None:
    assert_type(await User.objects.values_list("id", "email").all(_conn), list[tuple[Any, ...]])


async def _check_values_list_flat_returns_scalars() -> None:
    assert_type(await User.objects.values_list("id", flat=True).all(_conn), list[Any])


async def _check_chaining_preserves_model_type() -> None:
    # Chaining a model queryset keeps ``QuerySet[User]`` (so ``all`` stays list[User]).
    qs: QuerySet[User] = User.objects.filter(is_active=True).order_by("-id").limit(10)
    assert_type(await qs.all(_conn), list[User])


def test_import_smoke() -> None:
    """One real test so pytest has something to run in this module."""
    assert issubclass(User, ferrum.Model)
    assert User.objects is not None
