"""Security tests for FTS compile-path allowlists and bound parameters."""

from __future__ import annotations

from typing import Annotated

import pytest

import ferrum
from ferrum.errors import FerrumCompileError
from ferrum.models import Field

pytestmark = pytest.mark.security


class SecureDoc(ferrum.Model):
    id: int = 0
    search_vector: Annotated[ferrum.TSVector, Field(fts_config="english")] | None = None
    email: str = ""


class TestFtsCompileSecurity:
    def test_unknown_operator_rejected_at_python_layer(self) -> None:
        with pytest.raises(FerrumCompileError, match="not supported"):
            SecureDoc.objects.filter(search_vector__regex="evil")

    def test_match_on_non_fts_field_rejected(self) -> None:
        with pytest.raises(FerrumCompileError, match="not supported"):
            SecureDoc.objects.filter(email__match="x")

    def test_query_string_not_in_sql(self) -> None:
        pytest.importorskip("ferrum._native", reason="Rust extension not built")
        query_token = "probe-bound-param-value"  # noqa: S105 — not a credential; probes SQL binding
        compiled = SecureDoc.objects.filter(search_vector__match=query_token)._compile(
            dialect="postgres"
        )
        assert query_token not in compiled["sql_text"]
        assert compiled["bound_params"]

    def test_invalid_fts_config_rejected_at_model_build(self) -> None:
        with pytest.raises(ValueError, match="invalid fts_config"):

            class BadConfig(ferrum.Model):
                id: int = 0
                sv: Annotated[ferrum.TSVector, Field(fts_config="english;drop table")] | None = None
