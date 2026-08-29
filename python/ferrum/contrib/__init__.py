"""Ferrum optional integrations (``ferrum[fastapi]`` extra).

Modules in this package must not be imported by any core query-path module
(enforced by import-linter in CI: ``cli-isolation`` and ``contrib-isolation``
contracts; see ``.importlinter``).

This package ``__init__`` deliberately does NOT import ``fastapi`` or any
contrib submodule, so ``import ferrum.contrib`` is safe even when FastAPI is
not installed and never pulls FastAPI into core Ferrum's import graph.
"""

__all__: list[str] = []
