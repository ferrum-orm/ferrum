"""Unit tests for QuerySet danger-API guards.

These tests verify that unscoped delete() and update() are rejected without
explicit danger API calls (ARCHITECTURE.md §3 / AGENTS.md §3).
"""

from __future__ import annotations

import pytest

import ferrum
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
            await qs.delete(None)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_update_without_filter_raises(self) -> None:
        qs: QuerySet[FakeUser] = QuerySet(FakeUser)
        with pytest.raises(FerrumDangerApiError, match="danger_update_all"):
            await qs.update(None, email="new@example.com")  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_delete_with_filter_does_not_raise_danger_error(self) -> None:
        qs: QuerySet[FakeUser] = QuerySet(FakeUser)
        filtered = qs.filter(id=1)
        # Should raise FerrumConfigError (native ext absent) or NotImplementedError,
        # NOT FerrumDangerApiError.
        with pytest.raises((NotImplementedError, ferrum.FerrumConfigError)):
            await filtered.delete(None)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_update_with_filter_does_not_raise_danger_error(self) -> None:
        qs: QuerySet[FakeUser] = QuerySet(FakeUser)
        filtered = qs.filter(id=1)
        with pytest.raises((NotImplementedError, ferrum.FerrumConfigError)):
            await filtered.update(None, email="x@example.com")  # type: ignore[arg-type]

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

    # ------------------------------------------------------------------
    # Danger API existence and bypass semantics
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_danger_delete_all_does_not_raise_danger_api_error(self) -> None:
        """danger_delete_all() must NOT raise FerrumDangerApiError — it is the bypass."""
        from ferrum.errors import FerrumConfigError

        qs: QuerySet[FakeUser] = QuerySet(FakeUser)
        try:
            await qs.danger_delete_all(None)  # type: ignore[arg-type]
        except FerrumDangerApiError:
            pytest.fail(
                "danger_delete_all() raised FerrumDangerApiError — "
                "the bypass API must never trigger the danger guard"
            )
        except (FerrumConfigError, NotImplementedError):
            # Expected: extension not built or conn is None
            pass

    @pytest.mark.asyncio
    async def test_danger_update_all_does_not_raise_danger_api_error(self) -> None:
        """danger_update_all() must NOT raise FerrumDangerApiError — it is the bypass."""
        from ferrum.errors import FerrumConfigError

        qs: QuerySet[FakeUser] = QuerySet(FakeUser)
        try:
            await qs.danger_update_all(None, email="new@example.com")  # type: ignore[arg-type]
        except FerrumDangerApiError:
            pytest.fail(
                "danger_update_all() raised FerrumDangerApiError — "
                "the bypass API must never trigger the danger guard"
            )
        except (FerrumConfigError, NotImplementedError):
            # Expected: extension not built or conn is None
            pass

    def test_danger_delete_all_exists_on_queryset(self) -> None:
        """danger_delete_all() must be a callable attribute on QuerySet."""
        assert callable(getattr(QuerySet, "danger_delete_all", None)), (
            "QuerySet.danger_delete_all() is missing"
        )

    def test_danger_update_all_exists_on_queryset(self) -> None:
        """danger_update_all() must be a callable attribute on QuerySet."""
        assert callable(getattr(QuerySet, "danger_update_all", None)), (
            "QuerySet.danger_update_all() is missing"
        )

    # ------------------------------------------------------------------
    # Write-path danger API — named test assertions for the guard messages
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_update_without_filter_raises_danger_api_error(self) -> None:
        """MIG-5: update() without filter raises FerrumDangerApiError naming danger_update_all."""
        qs: QuerySet[FakeUser] = QuerySet(FakeUser)
        with pytest.raises(FerrumDangerApiError) as exc_info:
            await qs.update(email="hacked@example.com")
        assert "danger_update_all" in str(exc_info.value), (
            "FerrumDangerApiError message must reference danger_update_all() as the safe bypass"
        )

    @pytest.mark.asyncio
    async def test_delete_without_filter_raises_danger_api_error(self) -> None:
        """MIG-5: delete() without filter raises FerrumDangerApiError naming danger_delete_all."""
        qs: QuerySet[FakeUser] = QuerySet(FakeUser)
        with pytest.raises(FerrumDangerApiError) as exc_info:
            await qs.delete()
        assert "danger_delete_all" in str(exc_info.value), (
            "FerrumDangerApiError message must reference danger_delete_all() as the safe bypass"
        )

    @pytest.mark.asyncio
    async def test_create_with_valid_values_does_not_raise_danger_api_error(self) -> None:
        """MIG-5: create() is a single-record INSERT and must never trigger the danger guard.

        Only unscoped bulk operations (delete/update without filter) require the
        danger API. A single-record INSERT is always scoped by definition.
        """
        import unittest.mock as mock

        mock_conn = mock.MagicMock()
        qs: QuerySet[FakeUser] = QuerySet(FakeUser)
        try:
            await qs.create(mock_conn, id=1, email="new@example.com")
        except FerrumDangerApiError:
            pytest.fail(
                "create() raised FerrumDangerApiError — "
                "single-record INSERT must never trigger the danger guard"
            )
        except Exception:  # noqa: S110
            pass  # Expected: native extension not built or connection layer not wired

    @pytest.mark.asyncio
    async def test_danger_update_all_with_assignments_does_not_raise(self) -> None:
        """MIG-5: danger_update_all() with kwargs must not raise FerrumDangerApiError.

        Keyword arguments to danger_update_all() are the field assignments for the
        bulk update; they must be accepted without triggering any guard.
        """
        from ferrum.errors import FerrumConfigError

        qs: QuerySet[FakeUser] = QuerySet(FakeUser)
        try:
            await qs.danger_update_all(None, email="bulk@example.com", id=0)  # type: ignore[arg-type]
        except FerrumDangerApiError:
            pytest.fail(
                "danger_update_all() raised FerrumDangerApiError — "
                "the bypass API must never trigger the danger guard, even with kwargs"
            )
        except (FerrumConfigError, NotImplementedError):
            pass  # Expected: extension not built or conn is None
