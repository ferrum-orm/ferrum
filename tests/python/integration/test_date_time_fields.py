"""Integration test for date and time fields (asyncpg)."""

import pytest
from datetime import date, time as time_type, datetime
from decimal import Decimal
from typing import Annotated
from pydantic import Field

import ferrum


class DateTimeModel(ferrum.Model):
    """Model with date and time columns for testing."""
    id: int | None = None
    name: str
    birth_date: Annotated[date | None, Field()] = None
    wake_time: Annotated[time_type | None, Field()] = None
    created_at: Annotated[datetime | None, Field(generated=True)] = None
    
    class Meta:
        table = "datetime_test"


@pytest.mark.asyncio
async def test_date_field_insert_and_query(pg_conn):
    """Test that date fields work through insert, select, filter, and update."""
    driver = pg_conn._require_driver()
    
    # Create table
    await driver.execute("DROP TABLE IF EXISTS datetime_test CASCADE")
    await driver.execute("""
        CREATE TABLE datetime_test (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            birth_date DATE,
            wake_time TIME,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    
    # Insert with date
    obj = await DateTimeModel.objects.create(
        pg_conn,
        name="Ada Lovelace",
        birth_date=date(1815, 12, 10),
        wake_time=time_type(6, 30, 0),
    )
    
    assert obj.id is not None
    assert obj.birth_date == date(1815, 12, 10)
    assert obj.wake_time == time_type(6, 30, 0)
    assert isinstance(obj.birth_date, date)
    assert isinstance(obj.wake_time, time_type)
    
    # Query back
    found = await DateTimeModel.objects.get(pg_conn, id=obj.id)
    assert found.birth_date == date(1815, 12, 10)
    assert found.wake_time == time_type(6, 30, 0)
    
    # Filter by date
    matches = await DateTimeModel.objects.filter(birth_date=date(1815, 12, 10)).all(pg_conn)
    assert len(matches) == 1
    assert matches[0].name == "Ada Lovelace"
    
    # Update date
    updated = await DateTimeModel.objects.filter(id=obj.id).update(
        pg_conn, birth_date=date(1816, 1, 1)
    )
    assert updated == 1
    
    # Verify update
    check = await DateTimeModel.objects.get(pg_conn, id=obj.id)
    assert check.birth_date == date(1816, 1, 1)
    
    # Cleanup
    await driver.execute("DROP TABLE datetime_test CASCADE")


@pytest.mark.asyncio
async def test_null_date_fields(pg_conn):
    """Test that nullable date/time fields work correctly."""
    driver = pg_conn._require_driver()
    
    await driver.execute("DROP TABLE IF EXISTS datetime_test CASCADE")
    await driver.execute("""
        CREATE TABLE datetime_test (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            birth_date DATE,
            wake_time TIME,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    
    # Insert with NULL date
    obj = await DateTimeModel.objects.create(pg_conn, name="Unknown", birth_date=None)
    
    assert obj.birth_date is None
    
    # Query back
    found = await DateTimeModel.objects.get(pg_conn, id=obj.id)
    assert found.birth_date is None
    
    # Filter for NULL
    nulls = await DateTimeModel.objects.filter(birth_date__is_null=True).all(pg_conn)
    assert len(nulls) == 1
    
    # Cleanup
    await driver.execute("DROP TABLE datetime_test CASCADE")
