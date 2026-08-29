"""Make the repo-root ``benchmarks`` package importable without installation.

``benchmarks/`` is a standalone tooling package (not part of the ``ferrum``
distribution), so it is never on ``sys.path`` via the normal package install.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
