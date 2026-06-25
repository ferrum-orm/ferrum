"""Unit tests for ferrum sqlmigrate offline SQL rendering."""

from __future__ import annotations

from pathlib import Path

from ferrum.cli import sqlmigrate_cmd
from ferrum.migrations.base import Migration
from ferrum.migrations.operations import Column, CreateTable


def test_sqlmigrate_renders_create_table(tmp_path: Path) -> None:
    class InitMigration(Migration):
        operations = [
            CreateTable(
                "widgets",
                [Column("id", "BIGSERIAL", primary_key=True), Column("name", "TEXT", not_null=True)],
            ),
        ]

    path = tmp_path / "0001_widgets.py"
    path.write_text("# migration\n", encoding="utf-8")

    import ferrum.migrations.loader as loader

    original_load = loader.load_module

    def fake_load(p: Path) -> type[Migration]:
        return InitMigration

    loader.load_module = fake_load  # type: ignore[method-assign]
    try:
        exit_code = sqlmigrate_cmd.run_sqlmigrate(tmp_path, "0001_widgets")
    finally:
        loader.load_module = original_load  # type: ignore[method-assign]

    assert exit_code == 0
