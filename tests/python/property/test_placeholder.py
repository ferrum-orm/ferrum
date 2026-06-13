"""Property-based test placeholder.

Property-based tests use Hypothesis to verify invariants over arbitrary inputs.
They are tagged ``property`` and run with ``pytest -m property``.

Planned properties:
- Arbitrary filter chains never produce SQL with literal user values.
- Error taxonomy mapping is total (all PostgreSQL SQLSTATE codes map somewhere).
- QuerySet chaining never produces shared mutable state between clones.
"""

import pytest

pytestmark = pytest.mark.property


def test_property_placeholder() -> None:
    """Placeholder — replaced when IR and compile path land."""
    pytest.skip("Property tests implemented with compile path")
