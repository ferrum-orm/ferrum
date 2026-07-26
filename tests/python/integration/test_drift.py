"""Integration tests for read-only schema-fidelity comparison."""

from __future__ import annotations

import pytest

import ferrum
from ferrum.migrations.drift import detect_drift


@pytest.mark.integration
async def test_detect_drift_reports_clean_database(
    pg_conn: ferrum.connection.Connection,
) -> None:
    report = await detect_drift(pg_conn, models=[])
    assert report.has_drift is False
