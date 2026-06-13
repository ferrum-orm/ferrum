"""Ferrum — async ORM for Python with a Rust-powered core.

Public re-exports for the top-level ``ferrum`` namespace.
Import paths are stable API; internal module paths are not.
"""

from __future__ import annotations

__version__ = "0.1.0"
__all__ = [
    "FerrumCompileError",
    "FerrumConfigError",
    "FerrumConnectionError",
    "FerrumDatabaseError",
    "FerrumError",
    "FerrumIntegrityError",
    "FerrumMigrationError",
    "FerrumMultipleObjectsError",
    "FerrumNotFoundError",
    "FerrumSchemaError",
    "Field",
    "MigrationResult",
    "Model",
    "ModelConfig",
    "QuerySet",
    "clear_hooks",
    "connect",
    "register_hook",
]

from ferrum.connection import connect
from ferrum.errors import (
    FerrumCompileError,
    FerrumConfigError,
    FerrumConnectionError,
    FerrumDatabaseError,
    FerrumError,
    FerrumIntegrityError,
    FerrumMigrationError,
    FerrumMultipleObjectsError,
    FerrumNotFoundError,
    FerrumSchemaError,
)
from ferrum.hooks import clear_hooks, register_hook
from ferrum.migrations import MigrationResult
from ferrum.models import Field, Model, ModelConfig
from ferrum.queryset import QuerySet
