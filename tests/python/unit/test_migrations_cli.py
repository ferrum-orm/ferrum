"""Unit tests for the migration CLI dispatcher (ferrum.cli.migrations_cmd).

Focus: the ``confirm`` derivation in ``run_migrations`` — supplying a token
(via ``--token`` or ``FERRUM_MIGRATION_TOKEN``) implies ``confirm=True``. This
is a documented behavior (docs/getting-started.md §8 CLI note); these tests pin
it so the implication cannot change silently.

The async ``_apply`` is replaced with a capture stub so no database connection
is opened — the tests assert only the flags ``run_migrations`` derives and
forwards.
"""

from __future__ import annotations

import argparse

import pytest

import ferrum.cli.migrations_cmd as migrations_cmd


def _apply_args(*, token: str | None, confirm: bool) -> argparse.Namespace:
    return argparse.Namespace(
        mig_command="apply",
        token=token,
        confirm=confirm,
        do_dry_run=True,  # avoid needing a DSN/connection in _apply
        plan_file="plan.json",
        environment="development",
    )


@pytest.fixture
def captured_apply(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Replace _apply with a stub that records the kwargs it was called with."""
    captured: dict = {}

    async def _stub(**kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(migrations_cmd, "_apply", _stub)
    # Ensure no ambient token leaks in from the developer's environment.
    monkeypatch.delenv("FERRUM_MIGRATION_TOKEN", raising=False)
    return captured


def test_token_flag_implies_confirm(captured_apply: dict) -> None:
    """--token present must derive confirm=True even when --confirm is absent."""
    migrations_cmd.run_migrations(_apply_args(token="abc123", confirm=False))  # noqa: S106
    assert captured_apply["confirm"] is True


def test_token_env_var_implies_confirm(
    captured_apply: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FERRUM_MIGRATION_TOKEN present must derive confirm=True."""
    monkeypatch.setenv("FERRUM_MIGRATION_TOKEN", "env-token")
    migrations_cmd.run_migrations(_apply_args(token=None, confirm=False))
    assert captured_apply["confirm"] is True


def test_no_token_no_confirm_stays_false(captured_apply: dict) -> None:
    """Without a token or --confirm, confirm must remain False."""
    migrations_cmd.run_migrations(_apply_args(token=None, confirm=False))
    assert captured_apply["confirm"] is False


def test_explicit_confirm_without_token(captured_apply: dict) -> None:
    """--confirm alone (no token) must derive confirm=True."""
    migrations_cmd.run_migrations(_apply_args(token=None, confirm=True))
    assert captured_apply["confirm"] is True
