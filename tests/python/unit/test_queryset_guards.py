"""Unit tests for QuerySet danger-API guards.

These tests verify that unscoped delete() and update() are rejected without
explicit danger API calls (ARCHITECTURE.md §3 / AGENTS.md §3).
"""

from __future__ import annotations

import pytest

from ferrum.errors import FerrumDangerApiError
from ferrum.models import Model
from ferrum.queryset import QuerySet


class FakeUser(Model):
    id: int = 0
    email: str = ""


class TestDangerApiGuards:
    @pytest.mark.asyncio
    async def test_delete_without_filter_raises(self) -> None:
        qs: QuerySet[FakeUser] = QuerySet(FakeUser)
        with pytest.raises(FerrumDangerApiError, match="danger_delete_all"):
            await qs.delete()

    @pytest.mark.asyncio
    async def test_update_without_filter_raises(self) -> None:
        qs: QuerySet[FakeUser] = QuerySet(FakeUser)
        with pytest.raises(FerrumDangerApiError, match="danger_update_all"):
            await qs.update(email="new@example.com")

    @pytest.mark.asyncio
    async def test_delete_with_filter_does_not_raise_danger_error(self) -> None:
        qs: QuerySet[FakeUser] = QuerySet(FakeUser)
        filtered = qs.filter(id=1)
        # Should raise NotImplementedError (connection not wired), NOT FerrumDangerApiError
        with pytest.raises(NotImplementedError):
            await filtered.delete()

    @pytest.mark.asyncio
    async def test_update_with_filter_does_not_raise_danger_error(self) -> None:
        qs: QuerySet[FakeUser] = QuerySet(FakeUser)
        filtered = qs.filter(id=1)
        with pytest.raises(NotImplementedError):
            await filtered.update(email="x@example.com")

    def test_filter_returns_new_queryset(self) -> None:
        qs: QuerySet[FakeUser] = QuerySet(FakeUser)
        filtered = qs.filter(id=1)
        assert filtered is not qs
        assert filtered._is_filtered
        assert not qs._is_filtered

    def test_order_by_desc(self) -> None:
        qs: QuerySet[FakeUser] = QuerySet(FakeUser)
        ordered = qs.order_by("-id")
        assert ordered._order_by == [{"field": "id", "direction": "desc"}]

    def test_order_by_asc(self) -> None:
        qs: QuerySet[FakeUser] = QuerySet(FakeUser)
        ordered = qs.order_by("email")
        assert ordered._order_by == [{"field": "email", "direction": "asc"}]
