"""Migration file loader: scan, parse, and topologically sort migration modules."""

from __future__ import annotations

import importlib.util
import re
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from ferrum.migrations.base import Migration

_MIGRATION_FILE_PATTERN = re.compile(r"^\d{4}_[a-z0-9_]+\.py$")


@dataclass
class MigrationModule:
    name: str  # e.g. "0001_create_note"
    path: Path
    migration: type[Migration]  # the Migration subclass

    @property
    def dependencies(self) -> list[str]:
        return list(getattr(self.migration, "dependencies", []))


def load_module(path: Path) -> type[Migration]:
    """Import a migration .py file and return its Migration class.

    Raises ValueError if the file has no Migration class.
    """
    spec = importlib.util.spec_from_file_location(f"_ferrum_migration_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"Cannot load migration file {path}: invalid module spec")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    migration_cls = getattr(mod, "Migration", None)
    if migration_cls is None:
        raise ValueError(f"Migration file {path} has no Migration class")
    return cast(type[Migration], migration_cls)


def scan(migrations_dir: str | Path) -> list[MigrationModule]:
    """Scan *migrations_dir* for ``*.py`` migration files and return them in topo order.

    Files must match ``NNNN_slug.py`` (four digits, underscore, lowercase slug).
    Returns a list ordered by dependencies (topological sort).

    Raises:
        ValueError: on cycles or missing dependency references.
    """
    dir_path = Path(migrations_dir)
    if not dir_path.exists():
        return []

    modules: dict[str, MigrationModule] = {}
    for p in sorted(dir_path.glob("*.py")):
        if not _MIGRATION_FILE_PATTERN.match(p.name):
            continue
        name = p.stem
        cls = load_module(p)
        modules[name] = MigrationModule(name=name, path=p, migration=cls)

    return _topo_sort(modules)


def _topo_sort(modules: dict[str, MigrationModule]) -> list[MigrationModule]:
    """Kahn's algorithm topological sort over *modules*.

    Raises:
        ValueError: if a dependency is missing from *modules*.
        ValueError: if a dependency cycle is detected.
    """
    # Validate all declared dependencies exist in the scanned set.
    for name, mod in modules.items():
        for dep in mod.dependencies:
            if dep not in modules:
                raise ValueError(f"Migration {name!r} depends on {dep!r} which was not found")

    # Build adjacency: dep -> [dependents], and track in-degree per node.
    in_degree: dict[str, int] = dict.fromkeys(modules, 0)
    dependents: dict[str, list[str]] = {name: [] for name in modules}

    for name, mod in modules.items():
        for dep in mod.dependencies:
            dependents[dep].append(name)
            in_degree[name] += 1

    # Start queue with all nodes that have no dependencies.
    queue: deque[str] = deque(name for name, degree in in_degree.items() if degree == 0)
    # Preserve file-name order within the same dependency level for determinism.
    queue = deque(sorted(queue))

    result: list[MigrationModule] = []
    while queue:
        name = queue.popleft()
        result.append(modules[name])
        # Reduce in-degree for each node that depends on the one we just processed.
        newly_free = sorted(dep for dep in dependents[name] if in_degree[dep] - 1 == 0)
        for dep in dependents[name]:
            in_degree[dep] -= 1
        for dep in newly_free:
            queue.append(dep)

    if len(result) != len(modules):
        # Some nodes were never emitted — there must be a cycle.
        cycled = sorted(set(modules) - {m.name for m in result})
        raise ValueError(f"Dependency cycle detected among migrations: {', '.join(cycled)}")

    return result


def migrations_dir_default() -> Path:
    """Return the migrations directory from project config, falling back to cwd/migrations."""
    from ferrum.config import find_project_root, load_config

    root = find_project_root(Path.cwd())
    cfg = load_config(root)
    return root / cfg.migrations_dir
