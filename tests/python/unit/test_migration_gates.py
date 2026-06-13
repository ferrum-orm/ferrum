"""Unit tests for migration safety gates (MIG-2, MIG-5)."""

from __future__ import annotations

import pytest

from ferrum.errors import FerrumMigrationError
from ferrum.migrations.gates import check_destructive_gate, check_environment_gate
from ferrum.migrations.tokens import generate_token, validate_token


class TestTokens:
    def test_token_validates_correct_digest(self) -> None:
        digest = "abc123def456" * 4
        token = generate_token(digest)
        assert validate_token(token, digest)

    def test_token_rejects_wrong_digest(self) -> None:
        token = generate_token("digest_a" * 4)
        assert not validate_token(token, "digest_b" * 4)

    def test_token_malformed(self) -> None:
        assert not validate_token("notavalidtoken", "some_digest")


class TestDestructiveGate:
    def test_raises_without_token(self) -> None:
        with pytest.raises(FerrumMigrationError, match="confirmation token"):
            check_destructive_gate("some_digest" * 2, confirmation_token=None)

    def test_raises_with_wrong_token(self) -> None:
        wrong_token = generate_token("other_digest" * 4)
        with pytest.raises(FerrumMigrationError, match="does not match"):
            check_destructive_gate("right_digest" * 4, confirmation_token=wrong_token)

    def test_passes_with_correct_token(self) -> None:
        digest = "correct_digest_abc"
        token = generate_token(digest)
        check_destructive_gate(digest, confirmation_token=token)  # must not raise


class TestEnvironmentGate:
    def test_development_allows_without_declaration(self) -> None:
        check_environment_gate("development", "development")  # must not raise

    def test_production_requires_explicit_declaration(self) -> None:
        with pytest.raises(FerrumMigrationError, match="production"):
            check_environment_gate("development", "production")

    def test_production_passes_with_correct_declaration(self) -> None:
        check_environment_gate("production", "production")  # must not raise

    def test_staging_requires_explicit_declaration(self) -> None:
        with pytest.raises(FerrumMigrationError, match="staging"):
            check_environment_gate("development", "staging")
