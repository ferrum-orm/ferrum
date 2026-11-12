"""Regression tests: makemigrations never writes a DEFAULT that migrate rejects.

The generator (``_python_default_to_sql``) and the DDL check (``_col_def`` /
``alter_column``) share ``_is_allowed_default``. Round-trip tests go model →
plan op → migration-file source → ``ops`` objects → DDL.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any

import pytest

import ferrum
from ferrum.cli.makemigrations_cmd import _build_existing_state, _op_to_source, run_makemigrations
from ferrum.errors import FerrumMigrationError
from ferrum.migrations import loader
from ferrum.migrations import operations as ops
from ferrum.migrations.orchestrator import _op_to_sql, compute_plan


def _round_trip_sql(op: dict[str, Any]) -> str:
    """Render *op* as migration-file source, evaluate it back, compile to SQL."""
    source = _op_to_source(op).strip().rstrip(",")
    rebuilt = eval(source, {"ops": ops})  # noqa: S307 — source produced by our own writer
    return _op_to_sql(rebuilt.to_op_dict())


class NdTierThresholds(ferrum.Model):
    model_config = ferrum.ModelConfig(table="nd_tier_thresholds")
    version: Annotated[str, ferrum.Field(primary_key=True, max_length=16)]
    active_window_days: int = 14
    min_level: int = -1
    ratio: float = 0.05
    epsilon: float = 1e-06
    big: float = 1e20
    budget: Annotated[Decimal, ferrum.Field(max_digits=12, decimal_places=2)] = Decimal("0")
    fee: Annotated[Decimal, ferrum.Field(max_digits=12, decimal_places=2)] = Decimal("-2.50")


@pytest.mark.parametrize(
    ("column", "expected"),
    [
        ("active_window_days", '"active_window_days" INTEGER NOT NULL DEFAULT 14'),
        ("min_level", '"min_level" INTEGER NOT NULL DEFAULT -1'),
        ("ratio", '"ratio" REAL NOT NULL DEFAULT 0.05'),
        ("epsilon", '"epsilon" REAL NOT NULL DEFAULT 1e-06'),
        ("big", '"big" REAL NOT NULL DEFAULT 1e+20'),
        ("budget", '"budget" NUMERIC(12,2) NOT NULL DEFAULT 0'),
        ("fee", '"fee" NUMERIC(12,2) NOT NULL DEFAULT -2.50'),
    ],
)
def test_numeric_default_round_trips_to_ddl(column: str, expected: str) -> None:
    (create,) = compute_plan([NdTierThresholds], existing_tables={})["ops"]

    assert expected in _round_trip_sql(create)
    assert column in {c["name"] for c in create["columns"]}


def test_numeric_default_on_added_column_round_trips() -> None:
    class NdAddCol(ferrum.Model):
        model_config = ferrum.ModelConfig(table="nd_add_col")
        id: int
        retries: int = 14

    (add,) = compute_plan([NdAddCol], existing_tables={"nd_add_col": ["id"]})["ops"]

    assert '"retries" INTEGER NOT NULL DEFAULT 14' in _round_trip_sql(add)


def test_second_makemigrations_reports_no_default_changes(tmp_path: Path) -> None:
    (create,) = compute_plan([NdTierThresholds], existing_tables={})["ops"]
    migration = tmp_path / "0001_init.py"
    migration.write_text(
        "from ferrum.migrations import Migration\n"
        "from ferrum.migrations import operations as ops\n\n"
        "class Migration(Migration):\n"
        "    dependencies = []\n"
        f"    operations = [\n{_op_to_source(create)}\n    ]\n",
        encoding="utf-8",
    )

    state = _build_existing_state(loader.scan(tmp_path))

    assert compute_plan([NdTierThresholds], state)["ops"] == []


@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan")])
def test_non_finite_default_fails_at_makemigrations(bad: float) -> None:
    class NdNonFinite(ferrum.Model):
        model_config = ferrum.ModelConfig(table="nd_non_finite")
        id: int
        limit: float = bad

    with pytest.raises(FerrumMigrationError, match=r"'NdNonFinite' field 'limit'.*FERR-M001"):
        compute_plan([NdNonFinite], existing_tables={})


async def test_makemigrations_cli_reports_error_and_writes_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class NdCliNonFinite(ferrum.Model):
        model_config = ferrum.ModelConfig(table="nd_cli_non_finite")
        id: int
        limit: float = float("inf")

    monkeypatch.setattr(
        "ferrum.cli.makemigrations_cmd._get_all_model_subclasses", lambda: [NdCliNonFinite]
    )

    assert await run_makemigrations(tmp_path, "bad") == 2
    assert "'NdCliNonFinite' field 'limit'" in capsys.readouterr().err
    assert list(tmp_path.glob("*.py")) == []


@pytest.mark.parametrize(
    "default", ["14", "-1", "0.05", "1e-06", "1E+20", "-2.50", "0", "1", "''", "NOW()", "TRUE"]
)
def test_ddl_check_accepts_numbers_and_allowlisted_tokens(default: str) -> None:
    column = ops.Column("c", "INTEGER", default=default)

    sql = _op_to_sql({"kind": "create_table", "table": "t", "columns": [column.to_col_dict()]})

    assert f"DEFAULT {default}" in sql


@pytest.mark.parametrize(
    "default",
    [
        "1; DROP TABLE x",
        "1 OR 1=1",
        "0x10",
        "1e",
        "--1",
        "+1",
        ".5",
        "1.",
        "'pending'",
        "inf",
        "nan",
        "1 ",
    ],
)
def test_ddl_check_rejects_everything_else(default: str) -> None:
    create = {
        "kind": "create_table",
        "table": "t",
        "columns": [ops.Column("c", "INTEGER", default=default).to_col_dict()],
    }
    alter = {"kind": "alter_column", "table": "t", "column": "c", "default": default}

    with pytest.raises(FerrumMigrationError, match="FERR-M001"):
        _op_to_sql(create)
    with pytest.raises(FerrumMigrationError, match="FERR-M001"):
        _op_to_sql(alter)


def test_alter_column_accepts_numeric_default() -> None:
    op = ops.AlterColumn("t", "c", default="14").to_op_dict()

    assert 'ALTER COLUMN "c" SET DEFAULT 14' in _op_to_sql(op)
