"""SQLAlchemy-style SQL echo / verbose logging for local development.

Enable via:

- ``ferrum.enable_echo()`` / ``ferrum.enable_echo(verbose=True)``
- ``async with ferrum.connect(..., echo=True)`` / ``echo="debug"``
- Environment: ``FERRUM_ECHO=1`` (SQL only) or ``FERRUM_ECHO=debug`` (SQL + binds)

Echo is **local-dev only**. Bound parameter values are printed only in verbose /
``debug`` mode (analogous to SQLAlchemy ``echo`` showing the params tuple).
Generic ``DEBUG=1`` never enables Ferrum echo (same rule as observability tiers).
"""

from __future__ import annotations

import os
import sys
from typing import Any, Literal, TextIO

EchoLevel = Literal["off", "sql", "debug"]

_GLOBAL_LEVEL: EchoLevel = "off"
_STREAM: TextIO = sys.stderr


def _parse_env_level() -> EchoLevel | None:
    raw = os.environ.get("FERRUM_ECHO", "").strip().lower()
    if not raw:
        return None
    if raw in ("0", "false", "off", "no"):
        return "off"
    if raw in ("1", "true", "yes", "on", "sql"):
        return "sql"
    if raw in ("debug", "verbose", "2"):
        return "debug"
    return "sql"


def resolve_echo_level(conn_echo: bool | str | None = None) -> EchoLevel:
    """Resolve the effective echo level (connection > global > env)."""
    if conn_echo is not None:
        if conn_echo is False or conn_echo == "off":
            return "off"
        if conn_echo is True or conn_echo == "sql":
            return "sql"
        if conn_echo in ("debug", "verbose"):
            return "debug"
        return "sql"
    if _GLOBAL_LEVEL != "off":
        return _GLOBAL_LEVEL
    return _parse_env_level() or "off"


def enable_echo(*, verbose: bool = False, stream: TextIO | None = None) -> None:
    """Enable console SQL echo (SQLAlchemy-like).

    Args:
        verbose: When ``True``, also print bound parameter values (local-dev).
        stream: Output stream (default ``sys.stderr``).
    """
    global _GLOBAL_LEVEL, _STREAM
    _GLOBAL_LEVEL = "debug" if verbose else "sql"
    if stream is not None:
        _STREAM = stream


def disable_echo() -> None:
    """Disable the process-wide echo flag (env ``FERRUM_ECHO`` still applies)."""
    global _GLOBAL_LEVEL
    _GLOBAL_LEVEL = "off"


def echo_sql(
    *,
    sql: str,
    bound_params: list[Any] | None = None,
    param_type_summary: list[str] | None = None,
    model: str = "",
    operation: str = "",
    duration_ms: float | None = None,
    row_count: int | None = None,
    status: str = "ok",
    conn_echo: bool | str | None = None,
) -> None:
    """Print a compiled statement when echo is active.

    Never raises — echo must not break the query path.
    """
    level = resolve_echo_level(conn_echo)
    if level == "off":
        return
    try:
        parts = ["[ferrum]"]
        if operation or model:
            parts.append(f"{operation or 'query'} {model}".strip())
        if duration_ms is not None:
            parts.append(f"{duration_ms:.3f}ms")
        if row_count is not None:
            parts.append(f"rows={row_count}")
        if status != "ok":
            parts.append(f"status={status}")
        header = " ".join(parts)
        print(header, file=_STREAM)
        print(sql, file=_STREAM)
        if level == "debug":
            if bound_params is not None:
                print(f"[ferrum] params={bound_params!r}", file=_STREAM)
            elif param_type_summary:
                print(f"[ferrum] param_types={param_type_summary!r}", file=_STREAM)
        elif param_type_summary:
            print(f"[ferrum] param_types={param_type_summary!r}", file=_STREAM)
    except Exception:
        # Echo must never break the query path.
        return
