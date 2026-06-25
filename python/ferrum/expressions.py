"""Composable query expressions (``Q`` objects).

``Q`` builds boolean predicate trees that the QuerySet lowers into IR v2
``Predicate`` nodes for Rust compilation. No SQL is produced here.
"""

from __future__ import annotations

from copy import copy
from typing import Any


class Q:
    """Composable boolean filter used with ``QuerySet.filter`` / ``exclude``.

    Supports ``&`` (AND), ``|`` (OR), and ``~`` (NOT)::

        Q(active=True) & (Q(role="admin") | Q(role="staff"))
    """

    AND = "and"
    OR = "or"
    default = AND

    def __init__(
        self,
        *args: Q | dict[str, Any],
        _connector: str | None = None,
        _negated: bool = False,
        **kwargs: Any,  # noqa: ANN401
    ) -> None:
        self.children: list[Q | dict[str, Any]] = list(args)
        if kwargs:
            self.children.append(kwargs)
        self.connector = _connector if _connector is not None else self.default
        self.negated = _negated

    def __and__(self, other: Q) -> Q:
        return self._combine(other, self.AND)

    def __or__(self, other: Q) -> Q:
        return self._combine(other, self.OR)

    def __invert__(self) -> Q:
        q = copy(self)
        q.negated = not self.negated
        return q

    def _combine(self, other: Q, connector: str) -> Q:
        if not isinstance(other, Q):
            msg = "Q objects must be combined with other Q objects."
            raise TypeError(msg)
        if self.connector == connector and not self.negated:
            q = copy(self)
            q.children.append(other)
            return q
        return Q(self, other, _connector=connector)

    def __repr__(self) -> str:
        return f"Q({self.connector!r}, children={self.children!r}, negated={self.negated})"


def args_to_q(*args: Q | dict[str, Any], **kwargs: Any) -> Q | None:  # noqa: ANN401
    """Normalize ``filter`` / ``exclude`` positional and keyword args to a single ``Q``."""
    if not args and not kwargs:
        return None
    parts: list[Q | dict[str, Any]] = list(args)
    if kwargs:
        parts.append(kwargs)
    if len(parts) == 1:
        only = parts[0]
        if isinstance(only, Q):
            return only
        if isinstance(only, dict):
            return Q(**only)
        msg = f"Expected Q or keyword lookups, got {type(only)!r}."
        raise TypeError(msg)
    result: Q | None = None
    for part in parts:
        q = part if isinstance(part, Q) else Q(**part) if isinstance(part, dict) else Q(part)
        result = q if result is None else result & q
    return result
