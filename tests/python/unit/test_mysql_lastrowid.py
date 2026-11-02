"""Unit tests for MySQL lastrowid handling."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


class TestMysqlExecuteReturningLastrowid:
    """Test MySQL driver execute_returning_lastrowid method."""

    async def test_method_exists_on_mysql_driver(self) -> None:
        """MySQL driver has execute_returning_lastrowid method."""
        from ferrum.drivers.mysql import AsyncmyDriver

        driver = AsyncmyDriver("mysql://user:pass@localhost/db")
        assert hasattr(driver, "execute_returning_lastrowid")
        assert callable(driver.execute_returning_lastrowid)

    async def test_fetchrow_no_longer_has_insert_hack(self) -> None:
        """MySQL fetchrow no longer contains the old INSERT refetch hack."""
        import inspect

        from ferrum.drivers.mysql import AsyncmyDriver

        driver = AsyncmyDriver("mysql://user:pass@localhost/db")
        source = inspect.getsource(driver.fetchrow)
        # Old hack had: if upper.startswith("INSERT") and "RETURNING" not in upper
        assert "RETURNING" not in source or source.count("RETURNING") <= 2
        # Should not have the table regex matching logic
        assert "_INSERT_TABLE_RE" not in source
