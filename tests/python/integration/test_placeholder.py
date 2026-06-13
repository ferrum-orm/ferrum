"""Integration test placeholder.

Full integration tests require a live PostgreSQL instance and the compiled
ferrum._native extension. They are run with ``pytest -m integration``.

These tests will exercise:
- Full read/write path through the extension and real PostgreSQL.
- Hydration correctness (ADR-003).
- Migration apply/gates in a real DB.
- Panic injection: Rust panic → catchable FerrumInternalError (ERR-2).
- Cancellation: asyncio timeout at the await point (no cancel logic in Rust).
"""

import pytest

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_integration_placeholder() -> None:
    """Placeholder — replaced when connection layer lands."""
    pytest.skip("Integration tests require ferrum._native extension and PostgreSQL")
