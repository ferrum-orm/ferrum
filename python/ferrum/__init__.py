"""Ferrum — async ORM for Python with a Rust-powered core.

Public re-exports for the top-level ``ferrum`` namespace.
Import paths are stable API; internal module paths are not.
"""

from __future__ import annotations

__version__ = "0.1.17"
__all__ = [
    "Aggregate",
    "ConnectionRegistry",
    "CreateExtension",
    "CreateFunction",
    "CreatePolicy",
    "DisableRLS",
    "DropExtension",
    "DropFunction",
    "DropPolicy",
    "EnableRLS",
    "FerrumCompileError",
    "FerrumConfigError",
    "FerrumConnectionError",
    "FerrumDatabaseError",
    "FerrumDeferredFieldError",
    "FerrumError",
    "FerrumIntegrityError",
    "FerrumMigrationError",
    "FerrumMultipleObjectsError",
    "FerrumNotFoundError",
    "FerrumRelationNotLoadedError",
    "FerrumSchemaError",
    "FerrumTimeoutError",
    "Field",
    "FlatValuesListQuerySet",
    "ForeignKey",
    "FullTextIndex",
    "Index",
    "ManyToMany",
    "MigrationResult",
    "Model",
    "ModelConfig",
    "OneToOne",
    "PoolConfig",
    "Q",
    "QuerySet",
    "RetryPolicy",
    "ShardRouter",
    "TSVector",
    "Transaction",
    "ValuesListQuerySet",
    "ValuesQuerySet",
    "Vector",
    "clear_hooks",
    "connect",
    "contrib",
    "disable_echo",
    "enable_echo",
    "enable_metrics",
    "enable_opentelemetry",
    "get_metrics",
    "get_session_config",
    "observability",
    "platform_admin_transaction",
    "register_hook",
    "schema_transaction",
    "session",
    "set_session_config",
    "tenant_transaction",
]

from ferrum import contrib, observability, session
from ferrum.connection import Transaction, connect
from ferrum.echo import disable_echo, enable_echo
from ferrum.errors import (
    FerrumCompileError,
    FerrumConfigError,
    FerrumConnectionError,
    FerrumDatabaseError,
    FerrumDeferredFieldError,
    FerrumError,
    FerrumIntegrityError,
    FerrumMigrationError,
    FerrumMultipleObjectsError,
    FerrumNotFoundError,
    FerrumRelationNotLoadedError,
    FerrumSchemaError,
    FerrumTimeoutError,
)
from ferrum.expressions import Q
from ferrum.hooks import clear_hooks, register_hook
from ferrum.migrations import (
    CreateExtension,
    CreateFunction,
    CreatePolicy,
    DisableRLS,
    DropExtension,
    DropFunction,
    DropPolicy,
    EnableRLS,
    MigrationResult,
)
from ferrum.models import (
    Field,
    ForeignKey,
    FullTextIndex,
    Index,
    ManyToMany,
    Model,
    ModelConfig,
    OneToOne,
    TSVector,
    Vector,
)
from ferrum.observability import enable_metrics, enable_opentelemetry, get_metrics
from ferrum.queryset import (
    Aggregate,
    FlatValuesListQuerySet,
    QuerySet,
    ValuesListQuerySet,
    ValuesQuerySet,
)
from ferrum.routing import ConnectionRegistry, PoolConfig, ShardRouter
from ferrum.runtime import RetryPolicy
from ferrum.session import (
    current_setting as get_session_config,
)
from ferrum.session import (
    platform_admin_transaction,
    schema_transaction,
    tenant_transaction,
)
from ferrum.session import (
    set_config as set_session_config,
)
