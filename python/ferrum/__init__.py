"""Ferrum — async ORM for Python with a Rust-powered core.

Public re-exports for the top-level ``ferrum`` namespace.
Import paths are stable API; internal module paths are not.
"""

from __future__ import annotations

__version__ = "0.1.0"
__all__ = [
    "Model",
    "QuerySet",
    "connect",
    "FerrumError",
    "FerrumCompileError",
    "FerrumNotFoundError",
    "FerrumIntegrityError",
    "FerrumConnectionError",
]

from ferrum.errors import (
    FerrumCompileError,
    FerrumConnectionError,
    FerrumError,
    FerrumIntegrityError,
    FerrumNotFoundError,
)
from ferrum.models import Model
from ferrum.queryset import QuerySet
from ferrum.connection import connect
