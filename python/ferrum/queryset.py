"""Ferrum QuerySet: lazy, chainable, async query builder.

``QuerySet`` accumulates filter/order/limit/offset state and only touches the
database when a terminal coroutine is awaited. The terminal methods delegate
to the connection layer (asyncpg) and to the Rust compiler (ferrum._native).

Design constraints:
- No SQL building here. QuerySet only builds the IR dict.
- Danger API guards live here: ``delete()`` and ``update()`` without a filter
  raise ``FerrumDangerApiError``; callers must use ``danger_delete_all()`` /
  ``danger_update_all()`` explicitly (AGENTS.md §3).
- This module must NOT import ``ferrum.cli`` or ``ferrum.contrib`` (enforced by
  import-linter contract in CI).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Generic, TypeVar

from ferrum.errors import FerrumDangerApiError

if TYPE_CHECKING:
    from ferrum.models import Model

_M = TypeVar("_M", bound="Model")


class QuerySet(Generic[_M]):
    """Lazy, chainable query builder for a Ferrum model.

    All filter/order/limit/offset methods return a new ``QuerySet`` instance
    (immutable chaining). Terminal coroutines (``all``, ``get``, ``first``,
    ``count``, ``delete``, ``update``) are async and require an active connection.
    """

    def __init__(self, model: type[_M]) -> None:
        self._model = model
        self._filters: list[dict[str, Any]] = []
        self._order_by: list[dict[str, Any]] = []
        self._limit: int | None = None
        self._offset: int | None = None
        self._is_filtered: bool = False

    # ------------------------------------------------------------------
    # Chaining methods (return new QuerySet)
    # ------------------------------------------------------------------

    def filter(self, **kwargs: Any) -> QuerySet[_M]:
        """Add equality filter(s). Returns a new QuerySet."""
        qs = self._clone()
        for field, value in kwargs.items():
            qs._filters.append({"field": field, "operator": "eq", "value": value})
        qs._is_filtered = True
        return qs

    def order_by(self, *fields: str) -> QuerySet[_M]:
        """Set ORDER BY. Prefix field with '-' for DESC. Returns a new QuerySet."""
        qs = self._clone()
        for f in fields:
            if f.startswith("-"):
                qs._order_by.append({"field": f[1:], "direction": "desc"})
            else:
                qs._order_by.append({"field": f, "direction": "asc"})
        return qs

    def limit(self, count: int) -> QuerySet[_M]:
        """Set LIMIT. Returns a new QuerySet."""
        qs = self._clone()
        qs._limit = count
        return qs

    def offset(self, count: int) -> QuerySet[_M]:
        """Set OFFSET. Returns a new QuerySet."""
        qs = self._clone()
        qs._offset = count
        return qs

    # ------------------------------------------------------------------
    # Danger API guards (AGENTS.md §3 / ARCHITECTURE.md §3.9)
    # ------------------------------------------------------------------

    async def delete(self) -> int:
        """Delete matching rows. Requires at least one filter.

        Raises:
            FerrumDangerApiError: if called without any filter. Use
                ``danger_delete_all()`` for an unscoped delete.
        """
        if not self._is_filtered:
            raise FerrumDangerApiError(
                "Refusing unscoped delete(). Use QuerySet.danger_delete_all() "
                "to explicitly delete all rows in the table."
            )
        raise NotImplementedError("delete() implementation pending connection layer")

    async def danger_delete_all(self) -> int:
        """Delete ALL rows in the table without a filter.

        This is an explicit escape hatch. Prefer ``filter(...).delete()`` for
        scoped deletes. This method name is intentionally verbose to prevent
        accidental use.
        """
        raise NotImplementedError("danger_delete_all() implementation pending connection layer")

    async def update(self, **kwargs: Any) -> int:
        """Update matching rows. Requires at least one filter.

        Raises:
            FerrumDangerApiError: if called without any filter. Use
                ``danger_update_all()`` for an unscoped update.
        """
        if not self._is_filtered:
            raise FerrumDangerApiError(
                "Refusing unscoped update(). Use QuerySet.danger_update_all() "
                "to explicitly update all rows in the table."
            )
        raise NotImplementedError("update() implementation pending connection layer")

    async def danger_update_all(self, **kwargs: Any) -> int:
        """Update ALL rows in the table without a filter."""
        raise NotImplementedError("danger_update_all() implementation pending connection layer")

    # ------------------------------------------------------------------
    # Terminal coroutines (async)
    # ------------------------------------------------------------------

    async def all(self) -> list[_M]:
        """Fetch all matching rows."""
        raise NotImplementedError("all() implementation pending connection layer")

    async def get(self, **kwargs: Any) -> _M:
        """Fetch exactly one matching row or raise."""
        raise NotImplementedError("get() implementation pending connection layer")

    async def first(self) -> _M | None:
        """Fetch the first matching row or None."""
        raise NotImplementedError("first() implementation pending connection layer")

    async def count(self) -> int:
        """Return the count of matching rows."""
        raise NotImplementedError("count() implementation pending connection layer")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _clone(self) -> QuerySet[_M]:
        qs: QuerySet[_M] = QuerySet(self._model)
        qs._filters = list(self._filters)
        qs._order_by = list(self._order_by)
        qs._limit = self._limit
        qs._offset = self._offset
        qs._is_filtered = self._is_filtered
        return qs
