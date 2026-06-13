"""Unit tests for migration file loader (scan, load_module, _topo_sort).

Invariants covered:
- scan() on empty dir returns [].
- scan() on non-existent path returns [].
- scan() with one valid file returns [MigrationModule] with correct name.
- scan() ignores files not matching NNNN_slug.py pattern.
- scan() with two ordered files returns them in topological order.
- load_module() raises ValueError when the file has no Migration class.
- _topo_sort (via scan()) raises ValueError on missing dependency.
- _topo_sort (via scan()) raises ValueError on a dependency cycle.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ferrum.migrations.loader import MigrationModule, load_module, scan

# ---------------------------------------------------------------------------
# Helper: write a minimal migration file to tmp_path
# ---------------------------------------------------------------------------

_MIGRATION_TEMPLATE = """\
from ferrum.migrations import Migration


class Migration(Migration):
    dependencies = {deps!r}
    operations = []
"""


def _write_migration(dir_path: Path, filename: str, *, deps: list[str] | None = None) -> Path:
    """Write a valid migration file and return its path."""
    p = dir_path / filename
    p.write_text(_MIGRATION_TEMPLATE.format(deps=deps or []))
    return p


# ---------------------------------------------------------------------------
# scan() on empty/non-existent dirs
# ---------------------------------------------------------------------------


class TestScanEmptyAndMissing:
    def test_empty_dir_returns_empty_list(self, tmp_path: Path) -> None:
        result = scan(tmp_path)
        assert result == []

    def test_non_existent_dir_returns_empty_list(self, tmp_path: Path) -> None:
        missing = tmp_path / "does_not_exist"
        result = scan(missing)
        assert result == []


# ---------------------------------------------------------------------------
# scan() with a single valid file
# ---------------------------------------------------------------------------


class TestScanSingleFile:
    def test_single_valid_file_returns_one_module(self, tmp_path: Path) -> None:
        _write_migration(tmp_path, "0001_create_note.py")
        result = scan(tmp_path)
        assert len(result) == 1

    def test_single_file_has_correct_name(self, tmp_path: Path) -> None:
        _write_migration(tmp_path, "0001_create_note.py")
        result = scan(tmp_path)
        assert result[0].name == "0001_create_note"

    def test_single_file_result_is_migration_module(self, tmp_path: Path) -> None:
        _write_migration(tmp_path, "0001_create_note.py")
        result = scan(tmp_path)
        assert isinstance(result[0], MigrationModule)

    def test_single_file_path_attribute_matches_file(self, tmp_path: Path) -> None:
        p = _write_migration(tmp_path, "0001_create_note.py")
        result = scan(tmp_path)
        assert result[0].path == p


# ---------------------------------------------------------------------------
# scan() ignores files not matching NNNN_slug.py
# ---------------------------------------------------------------------------


class TestScanPatternFiltering:
    def test_non_matching_py_files_are_ignored(self, tmp_path: Path) -> None:
        (tmp_path / "__init__.py").write_text("")
        (tmp_path / "helper.py").write_text("")
        (tmp_path / "create_table.py").write_text("")  # no leading digits
        (tmp_path / "001_bad.py").write_text("")  # only 3 digits
        result = scan(tmp_path)
        assert result == []

    def test_valid_file_alongside_invalid_files(self, tmp_path: Path) -> None:
        _write_migration(tmp_path, "0001_create_note.py")
        (tmp_path / "README.md").write_text("docs")
        (tmp_path / "helper.py").write_text("# not a migration")
        result = scan(tmp_path)
        assert len(result) == 1
        assert result[0].name == "0001_create_note"


# ---------------------------------------------------------------------------
# scan() with two files — topological ordering
# ---------------------------------------------------------------------------


class TestScanTopologicalOrder:
    def test_two_ordered_files_returned_dependency_first(self, tmp_path: Path) -> None:
        _write_migration(tmp_path, "0001_create_note.py", deps=[])
        _write_migration(tmp_path, "0002_add_title.py", deps=["0001_create_note"])
        result = scan(tmp_path)
        assert len(result) == 2
        assert result[0].name == "0001_create_note"
        assert result[1].name == "0002_add_title"

    def test_three_files_chain_returned_in_order(self, tmp_path: Path) -> None:
        _write_migration(tmp_path, "0001_a.py", deps=[])
        _write_migration(tmp_path, "0002_b.py", deps=["0001_a"])
        _write_migration(tmp_path, "0003_c.py", deps=["0002_b"])
        result = scan(tmp_path)
        names = [m.name for m in result]
        assert names == ["0001_a", "0002_b", "0003_c"]

    def test_independent_migrations_both_included(self, tmp_path: Path) -> None:
        _write_migration(tmp_path, "0001_create_users.py", deps=[])
        _write_migration(tmp_path, "0002_create_posts.py", deps=[])
        result = scan(tmp_path)
        names = {m.name for m in result}
        assert names == {"0001_create_users", "0002_create_posts"}


# ---------------------------------------------------------------------------
# load_module() raises ValueError for missing Migration class
# ---------------------------------------------------------------------------


class TestLoadModule:
    def test_raises_value_error_when_no_migration_class(self, tmp_path: Path) -> None:
        p = tmp_path / "0001_bad.py"
        p.write_text("# no Migration class here\nx = 1\n")
        with pytest.raises(ValueError, match=r"[Mm]igration"):
            load_module(p)

    def test_returns_class_for_valid_file(self, tmp_path: Path) -> None:
        p = _write_migration(tmp_path, "0001_ok.py")
        cls = load_module(p)
        # The returned object must be a class and have a 'dependencies' attribute.
        assert isinstance(cls, type)
        assert hasattr(cls, "dependencies")

    def test_returned_class_has_correct_dependencies(self, tmp_path: Path) -> None:
        p = _write_migration(tmp_path, "0002_with_deps.py", deps=["0001_base"])
        cls = load_module(p)
        assert cls.dependencies == ["0001_base"]


# ---------------------------------------------------------------------------
# _topo_sort (via scan()) — missing dependency raises ValueError
# ---------------------------------------------------------------------------


class TestTopoSortMissingDependency:
    def test_missing_dependency_raises_value_error(self, tmp_path: Path) -> None:
        _write_migration(tmp_path, "0001_child.py", deps=["0099_nonexistent"])
        with pytest.raises(ValueError, match="0099_nonexistent"):
            scan(tmp_path)

    def test_missing_dep_message_names_offending_migration(self, tmp_path: Path) -> None:
        _write_migration(tmp_path, "0001_child.py", deps=["0099_ghost"])
        with pytest.raises(ValueError) as exc_info:
            scan(tmp_path)
        assert "0001_child" in str(exc_info.value)


# ---------------------------------------------------------------------------
# _topo_sort (via scan()) — cycle raises ValueError
# ---------------------------------------------------------------------------


class TestTopoSortCycle:
    def test_two_node_cycle_raises_value_error(self, tmp_path: Path) -> None:
        _write_migration(tmp_path, "0001_foo.py", deps=["0002_bar"])
        _write_migration(tmp_path, "0002_bar.py", deps=["0001_foo"])
        with pytest.raises(ValueError, match=r"[Cc]ycle"):
            scan(tmp_path)

    def test_cycle_message_mentions_involved_migrations(self, tmp_path: Path) -> None:
        _write_migration(tmp_path, "0001_a.py", deps=["0002_b"])
        _write_migration(tmp_path, "0002_b.py", deps=["0001_a"])
        with pytest.raises(ValueError) as exc_info:
            scan(tmp_path)
        msg = str(exc_info.value)
        assert "0001_a" in msg or "0002_b" in msg

    def test_three_node_cycle_raises_value_error(self, tmp_path: Path) -> None:
        _write_migration(tmp_path, "0001_x.py", deps=["0003_z"])
        _write_migration(tmp_path, "0002_y.py", deps=["0001_x"])
        _write_migration(tmp_path, "0003_z.py", deps=["0002_y"])
        with pytest.raises(ValueError, match=r"[Cc]ycle"):
            scan(tmp_path)
