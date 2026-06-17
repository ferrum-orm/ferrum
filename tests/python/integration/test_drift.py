"""Integration tests for schema drift detection (WS-B contract).

Drift detection compares live schema against ledger expectations. The API is
documented in PRODUCTION_READINESS_GAPS.md / WS-B but not yet exported from
``ferrum.migrations`` — tests xfail until implementation lands.
"""

from __future__ import annotations

import importlib

import pytest

import ferrum


def _drift_api_available() -> bool:
    migrations = importlib.import_module("ferrum.migrations")
    return hasattr(migrations, "detect_drift") or hasattr(migrations, "check_drift")


@pytest.mark.integration
@pytest.mark.xfail(
    not _drift_api_available(),
    reason="Schema drift detection not yet implemented (WS-B)",
    strict=True,
)
async def test_detect_drift_reports_clean_database(
    pg_conn: ferrum.connection.Connection,
) -> None:
    migrations = importlib.import_module("ferrum.migrations")
    detect = getattr(migrations, "detect_drift", None) or migrations.check_drift
    assert detect is not None

    report = await detect(pg_conn, models=[])
    assert report is not None
    assert getattr(report, "has_drift", False) is False
