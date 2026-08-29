"""Composable query expressions (``Q`` objects, ``F`` field refs, ``Star``).

``Q`` builds boolean predicate trees that the QuerySet lowers into IR v2
``Predicate`` nodes for Rust compilation. No SQL is produced here.

``F`` provides a typed field-reference expression for use in filters and
aggregate descriptions. ``Star`` is a marker for ``COUNT(*)``.

All expression types here compile through the **existing** IR v4 nodes —
no new IR nodes are introduced.  Features that require new IR nodes
(window functions, CTEs, UNION, scalar subqueries, CASE WHEN, database
functions) are documented as blockers pending ChiefArchitect escalation.
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


# ---------------------------------------------------------------------------
# Reusable expression types (compile through existing IR v4 nodes)
# ---------------------------------------------------------------------------


class Combinable:
    """Base class for composable query expressions.

    Subclasses provide typed representations of SQL concepts that Ferrum
    lowers through the existing IR.  They never carry raw SQL fragments;
    all identifiers are resolved against model metadata allowlists at
    compile time.
    """


class F(Combinable):
    """A typed reference to a model field.

    ``F`` improves readability and type-safety when referencing fields in
    filter or aggregate contexts.  It resolves to the same metadata-
    allowlisted field index as a plain string field name::

        from ferrum.expressions import F

        User.objects.filter(email="x@y.com")        # plain string
        User.objects.filter(**{F("email"): "x@y.com"})  # typed F

    ``F`` does **not** enable field-to-field comparison (``F("a") > F("b")``)
    — that requires a new IR node and is tracked as a blocker.
    """

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        if not isinstance(name, str) or not name:
            msg = f"F() requires a non-empty string, got {name!r}."
            raise ValueError(msg)
        self.name = name

    def __repr__(self) -> str:
        return f"F({self.name!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, F) and self.name == other.name

    def __hash__(self) -> int:
        return hash((F, self.name))


class Star(Combinable):
    """Marker for ``COUNT(*)`` — pass to ``Aggregate.count()`` for explicit intent.

    ``Aggregate.count(Star())`` and ``Aggregate.count()`` produce identical
    SQL (``COUNT(*)``).  ``Star`` makes the intent visible at the call site::

        from ferrum.expressions import Star
        from ferrum import Aggregate

        qs.aggregate(conn, total=Aggregate.count(Star()))
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "Star()"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Star)

    def __hash__(self) -> int:
        return hash(Star)


def resolve_field_name(value: str | F) -> str:
    """Return the field name from a plain string or an ``F`` expression.

    Raises ``TypeError`` for any other type — never accepts raw SQL.
    """
    if isinstance(value, F):
        return value.name
    if isinstance(value, str):
        return value
    msg = f"Field reference must be str or F, got {type(value).__name__}."
    raise TypeError(msg)
