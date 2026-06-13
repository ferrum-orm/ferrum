"""Ferrum — async ORM for Python with a Rust-powered core.

Public re-exports for the top-level ``ferrum`` namespace.
Import paths are stable API; internal module paths are not.
"""

from __future__ import annotations

__version__ = "0.1.0"
__all__ = [
    "FerrumCompileError",
    "FerrumConnectionError",
    "FerrumError",
    "FerrumIntegrityError",
    "FerrumNotFoundError",
    "Model",
    "QuerySet",
    "connect",
]

from ferrum.connection import connect
from ferrum.errors import (
    FerrumCompileError,
    FerrumConnectionError,
    FerrumError,
    FerrumIntegrityError,
    FerrumNotFoundError,
)
from ferrum.models import Model
from ferrum.queryset import QuerySet
