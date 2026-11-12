"""Regression tests for makemigrations FK targets, FK order, composite PKs and enum CHECKs.

Each round-trip test renders a plan op with the makemigrations source writer,
evaluates that source back into ``ops`` objects, and compiles it to SQL, so a
field the writer drops fails the test.
"""

from __future__ import annotations

from typing import Annotated, Any, ClassVar, Literal

import pytest

import ferrum
from ferrum.cli.makemigrations_cmd import _build_reverse_block, _op_to_source
from ferrum.errors import FerrumSchemaError
from ferrum.migrations import operations as ops
from ferrum.migrations.drift import _canonical_check, _compare_constraints
from ferrum.migrations.orchestrator import _op_to_sql, compute_plan


def _round_trip(op: dict[str, Any]) -> dict[str, Any]:
    """Render *op* as migration-file source and evaluate it back to an op dict."""
    source = _op_to_source(op).strip().rstrip(",")
    rebuilt = eval(source, {"ops": ops})  # noqa: S307 — source produced by our own writer
    return rebuilt.to_op_dict()


def _ops_of(plan: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [op for op in plan["ops"] if op["kind"] == kind]


# ---------------------------------------------------------------------------
# Bug 1: FK references the target's primary-key column, not a hard-coded "id".
# ---------------------------------------------------------------------------


def test_fk_references_natural_primary_key() -> None:
    class FkpcCatalog(ferrum.Model):
        model_config = ferrum.ModelConfig(table="fkpc_catalog")
        version: Annotated[str, ferrum.Field(primary_key=True, max_length=16)]

    class FkpcActive(ferrum.Model):
        model_config = ferrum.ModelConfig(table="fkpc_active")
        id: Annotated[int, ferrum.Field(primary_key=True)] = 1
        catalog_version: str = ferrum.Field(max_length=16)
        catalog: ClassVar[ferrum.ForeignKey] = ferrum.ForeignKey(
            to="FkpcCatalog", db_column="catalog_version", on_delete="RESTRICT"
        )

    plan = compute_plan([FkpcActive, FkpcCatalog], existing_tables={})
    (fk,) = _ops_of(plan, "add_fk")

    assert (fk["ref_table"], fk["ref_column"]) == ("fkpc_catalog", "version")
    sql = _op_to_sql(_round_trip(fk))
    assert 'REFERENCES "fkpc_catalog" ("version") ON DELETE RESTRICT' in sql


def test_fk_to_id_primary_key_is_unchanged() -> None:
    class FkpcAuthor(ferrum.Model):
        model_config = ferrum.ModelConfig(table="fkpc_author")
        id: int

    class FkpcBook(ferrum.Model):
        model_config = ferrum.ModelConfig(table="fkpc_book")
        id: int
        author_id: int
        author: ClassVar[ferrum.ForeignKey] = ferrum.ForeignKey(
            to="FkpcAuthor", db_column="author_id"
        )

    (fk,) = _ops_of(compute_plan([FkpcAuthor, FkpcBook], existing_tables={}), "add_fk")

    assert fk == {
        "kind": "add_fk",
        "table": "fkpc_book",
        "name": "fk_fkpc_book_author_id",
        "column": "author_id",
        "ref_table": "fkpc_author",
        "ref_column": "id",
        "on_delete": "CASCADE",
    }


def test_fk_to_composite_primary_key_raises_schema_error() -> None:
    class FkpcComposite(ferrum.Model):
        model_config = ferrum.ModelConfig(table="fkpc_composite")
        a: Annotated[str, ferrum.Field(primary_key=True, max_length=8)]
        b: Annotated[str, ferrum.Field(primary_key=True, max_length=8)]

    class FkpcRefersComposite(ferrum.Model):
        model_config = ferrum.ModelConfig(table="fkpc_refers_composite")
        id: int
        a: str = ferrum.Field(max_length=8)
        parent: ClassVar[ferrum.ForeignKey] = ferrum.ForeignKey(to="FkpcComposite", db_column="a")

    with pytest.raises(FerrumSchemaError, match=r"composite primary key \(a, b\)"):
        compute_plan([FkpcComposite, FkpcRefersComposite], existing_tables={})


# ---------------------------------------------------------------------------
# Bug 2: every new table exists before any FK that references it.
# ---------------------------------------------------------------------------


def test_fk_to_model_declared_later_comes_after_its_table() -> None:
    class FkpcLateChild(ferrum.Model):
        model_config = ferrum.ModelConfig(table="fkpc_late_child")
        id: int
        parent_id: int
        parent: ClassVar[ferrum.ForeignKey] = ferrum.ForeignKey(
            to="FkpcLateParent", db_column="parent_id"
        )

    class FkpcLateParent(ferrum.Model):
        model_config = ferrum.ModelConfig(table="fkpc_late_parent")
        id: int

    kinds = [
        (op["kind"], op["table"])
        for op in compute_plan([FkpcLateChild, FkpcLateParent], existing_tables={})["ops"]
    ]

    assert kinds == [
        ("create_table", "fkpc_late_child"),
        ("create_table", "fkpc_late_parent"),
        ("add_fk", "fkpc_late_child"),
    ]


def test_self_referencing_fk_comes_after_its_table() -> None:
    class FkpcNode(ferrum.Model):
        model_config = ferrum.ModelConfig(table="fkpc_node")
        id: int
        parent_id: int | None = None
        parent: ClassVar[ferrum.ForeignKey] = ferrum.ForeignKey(
            to="FkpcNode", db_column="parent_id"
        )

    plan_ops = compute_plan([FkpcNode], existing_tables={})["ops"]

    assert [op["kind"] for op in plan_ops] == ["create_table", "add_fk"]
    assert (plan_ops[1]["ref_table"], plan_ops[1]["ref_column"]) == ("fkpc_node", "id")


def test_reverse_operations_drop_fks_before_tables() -> None:
    class FkpcRevParent(ferrum.Model):
        model_config = ferrum.ModelConfig(table="fkpc_rev_parent")
        id: int

    class FkpcRevChild(ferrum.Model):
        model_config = ferrum.ModelConfig(table="fkpc_rev_child")
        id: int
        parent_id: int
        parent: ClassVar[ferrum.ForeignKey] = ferrum.ForeignKey(
            to="FkpcRevParent", db_column="parent_id"
        )

    block = _build_reverse_block(
        compute_plan([FkpcRevChild, FkpcRevParent], existing_tables={})["ops"]
    )
    lines = [line.strip() for line in block.splitlines() if line.strip().startswith("ops.")]

    assert lines == [
        "ops.DropForeignKey('fkpc_rev_child', 'fk_fkpc_rev_child_parent_id'),",
        "ops.DropTable('fkpc_rev_parent'),",
        "ops.DropTable('fkpc_rev_child'),",
    ]


# ---------------------------------------------------------------------------
# Bug 3: composite primary keys survive the generated migration file.
# ---------------------------------------------------------------------------


def test_composite_primary_key_round_trips_through_generated_source() -> None:
    class FkpcEntry(ferrum.Model):
        model_config = ferrum.ModelConfig(table="fkpc_entry")
        version: Annotated[str, ferrum.Field(primary_key=True, max_length=16)]
        level_id: Annotated[int, ferrum.Field(primary_key=True)]
        plays_cnt: int

    (create,) = _ops_of(compute_plan([FkpcEntry], existing_tables={}), "create_table")
    rebuilt = _round_trip(create)

    assert rebuilt["composite_pk_columns"] == ["version", "level_id"]
    sql = _op_to_sql(rebuilt)
    assert 'PRIMARY KEY ("version", "level_id")' in sql
    assert sql.count("PRIMARY KEY") == 1


# ---------------------------------------------------------------------------
# Bug 4: Literal (enum) CHECK constraints survive the generated migration file.
# ---------------------------------------------------------------------------


def test_enum_check_round_trips_through_generated_source() -> None:
    class FkpcStatus(ferrum.Model):
        model_config = ferrum.ModelConfig(table="fkpc_status")
        id: int
        status: Literal["candidate", "active", "rejected", "retired"] = "candidate"

    (create,) = _ops_of(compute_plan([FkpcStatus], existing_tables={}), "create_table")
    rebuilt = _round_trip(create)

    sql = _op_to_sql(rebuilt)
    assert (
        """"status" TEXT NOT NULL CHECK ("status" IN """
        """('candidate', 'active', 'rejected', 'retired'))"""
    ) in sql


def test_enum_check_on_added_column_round_trips() -> None:
    class FkpcAddEnum(ferrum.Model):
        model_config = ferrum.ModelConfig(table="fkpc_add_enum")
        id: int
        kind: Literal["a", "b"] | None = None

    (add,) = _ops_of(
        compute_plan([FkpcAddEnum], existing_tables={"fkpc_add_enum": ["id"]}), "add_column"
    )

    assert """CHECK ("kind" IN ('a', 'b'))""" in _op_to_sql(_round_trip(add))


def test_enum_check_escapes_single_quotes() -> None:
    column = ops.Column("note", "TEXT", enum_values=["it's", "ok"])

    sql = _op_to_sql({"kind": "create_table", "table": "t", "columns": [column.to_col_dict()]})

    assert """CHECK ("note" IN ('it''s', 'ok'))""" in sql


# ---------------------------------------------------------------------------
# Drift: a correctly generated schema is not reported as drift.
# ---------------------------------------------------------------------------


def test_drift_check_matches_postgres_any_array_form() -> None:
    model_form = "CHECK (status IN ('candidate', 'active'))"
    live_form = "CHECK ((status = ANY (ARRAY['candidate'::text, 'active'::text])))"

    assert _canonical_check(model_form) == _canonical_check(live_form)
    assert _canonical_check(model_form) != _canonical_check(
        "CHECK ((status = ANY (ARRAY['candidate'::text, 'retired'::text])))"
    )


@pytest.mark.parametrize("live_name", ["fk_orders_customer_id", "orders_customer_id_fkey"])
def test_drift_fk_matches_by_column_under_either_name(live_name: str) -> None:
    expected = [
        {
            "name": "fk_orders_customer_id",
            "type": "FOREIGN KEY",
            "column": "customer_id",
            "delete_rule": "CASCADE",
        }
    ]
    actual = [
        {
            "name": live_name,
            "type": "FOREIGN KEY",
            "column": "customer_id",
            "foreign_table": "customer",
            "foreign_column": "id",
            "delete_rule": "CASCADE",
            "definition": "FOREIGN KEY (customer_id) REFERENCES customer(id) ON DELETE CASCADE",
        }
    ]

    assert _compare_constraints("orders", expected, actual) == ()
