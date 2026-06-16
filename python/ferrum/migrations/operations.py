"""Migration operation classes for Ferrum.

Each class serialises to the exact ``dict`` shape that ``_op_to_sql()`` in
``orchestrator.py`` already accepts.  No SQL is built here — that responsibility
stays inside ``orchestrator._op_to_sql``.

Security note: operation objects must only be constructed from model-metadata
allowlists or explicit developer-supplied literals in migration files.  They are
not safe endpoints for user input.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class Operation(ABC):
    """Abstract base for all migration operations."""

    @abstractmethod
    def to_op_dict(self) -> dict[str, Any]:
        """Return the op dict accepted by ``_op_to_sql``."""

    @property
    @abstractmethod
    def classification(self) -> str:
        """Return ``"safe"``, ``"destructive"``, or ``"non_transactional"``."""


class Column:
    """Descriptor for a table column.  Not an operation; used by :class:`CreateTable`
    and :class:`AddColumn`.
    """

    def __init__(
        self,
        name: str,
        sql_type: str,
        *,
        not_null: bool = False,
        default: str | None = None,
        primary_key: bool = False,
    ) -> None:
        self.name = name
        self.sql_type = sql_type
        self.not_null = not_null
        self.default = default
        self.primary_key = primary_key

    def to_col_dict(self) -> dict[str, Any]:
        """Return a column-def dict consumed by ``_col_def`` / ``_op_to_sql``."""
        return {
            "name": self.name,
            "sql_type": self.sql_type,
            "not_null": self.not_null,
            "default": self.default,
            "primary_key": self.primary_key,
        }

    def __repr__(self) -> str:
        flags = []
        if self.not_null:
            flags.append("not_null=True")
        if self.primary_key:
            flags.append("primary_key=True")
        if self.default is not None:
            flags.append(f"default={self.default!r}")
        extra = (", " + ", ".join(flags)) if flags else ""
        return f"Column({self.name!r}, {self.sql_type!r}{extra})"


class CreateTable(Operation):
    """Create a new table with the given columns."""

    def __init__(self, table_name: str, columns: list[Column]) -> None:
        self.table_name = table_name
        self.columns = columns

    def to_op_dict(self) -> dict[str, Any]:
        return {
            "kind": "create_table",
            "table": self.table_name,
            "columns": [c.to_col_dict() for c in self.columns],
        }

    @property
    def classification(self) -> str:
        return "safe"

    def __repr__(self) -> str:
        return f"CreateTable({self.table_name!r}, {self.columns!r})"


class DropTable(Operation):
    """Drop an existing table (destructive)."""

    def __init__(self, table_name: str) -> None:
        self.table_name = table_name

    def to_op_dict(self) -> dict[str, Any]:
        return {"kind": "drop_table", "table": self.table_name}

    @property
    def classification(self) -> str:
        return "destructive"

    def __repr__(self) -> str:
        return f"DropTable({self.table_name!r})"


class AddColumn(Operation):
    """Add a column to an existing table.

    ``_op_to_sql`` for ``add_column`` calls ``_col_def(op)`` with the op dict
    itself, so column fields are **flattened** into the top-level dict alongside
    ``kind`` and ``table``.
    """

    def __init__(self, table_name: str, column: Column) -> None:
        self.table_name = table_name
        self.column = column

    def to_op_dict(self) -> dict[str, Any]:
        return {
            "kind": "add_column",
            "table": self.table_name,
            **self.column.to_col_dict(),
        }

    @property
    def classification(self) -> str:
        return "safe"

    def __repr__(self) -> str:
        return f"AddColumn({self.table_name!r}, {self.column!r})"


class DropColumn(Operation):
    """Drop a column from an existing table (destructive)."""

    def __init__(self, table_name: str, column_name: str) -> None:
        self.table_name = table_name
        self.column_name = column_name

    def to_op_dict(self) -> dict[str, Any]:
        return {
            "kind": "drop_column",
            "table": self.table_name,
            "column": self.column_name,
        }

    @property
    def classification(self) -> str:
        return "destructive"

    def __repr__(self) -> str:
        return f"DropColumn({self.table_name!r}, {self.column_name!r})"


class RenameColumn(Operation):
    """Rename a column in an existing table."""

    def __init__(self, table_name: str, from_name: str, to_name: str) -> None:
        self.table_name = table_name
        self.from_name = from_name
        self.to_name = to_name

    def to_op_dict(self) -> dict[str, Any]:
        return {
            "kind": "rename_column",
            "table": self.table_name,
            "from": self.from_name,
            "to": self.to_name,
        }

    @property
    def classification(self) -> str:
        return "safe"

    def __repr__(self) -> str:
        return f"RenameColumn({self.table_name!r}, {self.from_name!r}, {self.to_name!r})"


class AddIndex(Operation):
    """Create an index on one or more columns."""

    def __init__(
        self,
        table_name: str,
        index_name: str,
        columns: list[str],
        *,
        unique: bool = False,
        using: str = "btree",
        where: str | None = None,
        opclasses: list[str] | None = None,
    ) -> None:
        self.table_name = table_name
        self.index_name = index_name
        self.columns = list(columns)
        self.unique = unique
        self.using = using
        self.where = where
        self.opclasses = list(opclasses) if opclasses is not None else None

    def to_op_dict(self) -> dict[str, Any]:
        op: dict[str, Any] = {
            "kind": "add_index",
            "table": self.table_name,
            "name": self.index_name,
            "columns": list(self.columns),
            "unique": self.unique,
            "using": self.using,
        }
        if self.where is not None:
            op["where"] = self.where
        if self.opclasses is not None:
            op["opclasses"] = list(self.opclasses)
        return op

    @property
    def classification(self) -> str:
        return "safe"

    def __repr__(self) -> str:
        extra = ", unique=True" if self.unique else ""
        return f"AddIndex({self.table_name!r}, {self.index_name!r}, {self.columns!r}{extra})"


class DropIndex(Operation):
    """Drop an existing index by name."""

    def __init__(self, index_name: str) -> None:
        self.index_name = index_name

    def to_op_dict(self) -> dict[str, Any]:
        return {"kind": "drop_index", "name": self.index_name}

    @property
    def classification(self) -> str:
        return "safe"

    def __repr__(self) -> str:
        return f"DropIndex({self.index_name!r})"


class AddForeignKey(Operation):
    """Add a foreign-key constraint to an existing table.

    Generates ``ALTER TABLE … ADD CONSTRAINT … FOREIGN KEY … REFERENCES … ON DELETE …``.
    The ``on_delete`` value is validated against the orchestrator allowlist before
    it is interpolated into DDL.
    """

    def __init__(
        self,
        table_name: str,
        constraint_name: str,
        column: str,
        ref_table: str,
        ref_column: str = "id",
        *,
        on_delete: str = "CASCADE",
    ) -> None:
        self.table_name = table_name
        self.constraint_name = constraint_name
        self.column = column
        self.ref_table = ref_table
        self.ref_column = ref_column
        self.on_delete = on_delete

    def to_op_dict(self) -> dict[str, Any]:
        return {
            "kind": "add_fk",
            "table": self.table_name,
            "name": self.constraint_name,
            "column": self.column,
            "ref_table": self.ref_table,
            "ref_column": self.ref_column,
            "on_delete": self.on_delete,
        }

    @property
    def classification(self) -> str:
        return "safe"

    def __repr__(self) -> str:
        return (
            f"AddForeignKey({self.table_name!r}, {self.constraint_name!r}, "
            f"{self.column!r} -> {self.ref_table!r}.{self.ref_column!r})"
        )


class DropForeignKey(Operation):
    """Drop a foreign-key constraint by name (destructive — removes referential integrity)."""

    def __init__(self, table_name: str, constraint_name: str) -> None:
        self.table_name = table_name
        self.constraint_name = constraint_name

    def to_op_dict(self) -> dict[str, Any]:
        return {"kind": "drop_fk", "table": self.table_name, "name": self.constraint_name}

    @property
    def classification(self) -> str:
        return "destructive"

    def __repr__(self) -> str:
        return f"DropForeignKey({self.table_name!r}, {self.constraint_name!r})"


class RawSQL(Operation):
    """Execute a raw SQL statement.

    ``safe=True`` indicates the statement was reviewed and requires no additional
    confirmation gate.  ``safe=False`` (default) will trigger the destructive-op
    confirmation gate in ``orchestrator.apply()``.
    """

    def __init__(self, sql: str, *, safe: bool = False) -> None:
        self.sql = sql
        self.safe = safe

    def to_op_dict(self) -> dict[str, Any]:
        return {"kind": "raw_sql", "sql": self.sql, "safe": self.safe}

    @property
    def classification(self) -> str:
        return "safe"

    def __repr__(self) -> str:
        safe_str = ", safe=True" if self.safe else ""
        return f"RawSQL({self.sql!r}{safe_str})"
