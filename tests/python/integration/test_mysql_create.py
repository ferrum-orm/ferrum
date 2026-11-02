"""MySQL integration tests for auto-increment PK handling in create/bulk_create."""

from __future__ import annotations

import os
from decimal import Decimal

import pytest
from pydantic import Field

import ferrum
from ferrum.errors import FerrumConfigError

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.integration,
    pytest.mark.skipif(
        "FERRUM_DATABASE_URL_MYSQL" not in os.environ,
        reason="MySQL DSN not configured",
    ),
]


class Product(ferrum.Model):
    """Test model with auto-increment PK."""

    id: int = Field(default=0)
    name: str = Field(max_length=100)
    price: Decimal = Field(max_digits=10, decimal_places=2)

    class Meta:
        table = "products"


class Item(ferrum.Model):
    """Test model with user-supplied PK."""

    code: str = Field(max_length=20)
    description: str = Field(max_length=200)

    class Meta:
        table = "items"
        pk_fields = ("code",)


@pytest.fixture
async def mysql_conn():
    """MySQL connection fixture."""
    dsn = os.environ["FERRUM_DATABASE_URL_MYSQL"]
    async with ferrum.connect(dsn) as conn:
        driver = conn._require_driver()
        # Drop and create test tables
        await driver.execute("DROP TABLE IF EXISTS products")
        await driver.execute("DROP TABLE IF EXISTS items")
        await driver.execute("""
            CREATE TABLE products (
                id INT AUTO_INCREMENT PRIMARY KEY,
                name VARCHAR(100) NOT NULL,
                price DECIMAL(10,2) NOT NULL
            )
        """)
        await driver.execute("""
            CREATE TABLE items (
                code VARCHAR(20) PRIMARY KEY,
                description VARCHAR(200) NOT NULL
            )
        """)
        yield conn
        # Cleanup
        await driver.execute("DROP TABLE IF EXISTS products")
        await driver.execute("DROP TABLE IF EXISTS items")


class TestMysqlCreateAutoIncrement:
    """Test MySQL create() with auto-increment PK."""

    async def test_create_returns_instance_with_auto_pk(self, mysql_conn) -> None:
        """create() returns instance with populated auto-increment PK."""
        product = await Product.objects.create(
            mysql_conn,
            name="Widget",
            price=Decimal("19.99"),
        )

        assert product.id > 0
        assert product.name == "Widget"
        assert product.price == Decimal("19.99")

    async def test_create_consecutive_auto_pks(self, mysql_conn) -> None:
        """Multiple create() calls get consecutive auto-increment PKs."""
        p1 = await Product.objects.create(
            mysql_conn,
            name="Widget A",
            price=Decimal("10.00"),
        )
        p2 = await Product.objects.create(
            mysql_conn,
            name="Widget B",
            price=Decimal("20.00"),
        )

        assert p1.id > 0
        assert p2.id == p1.id + 1

    async def test_create_with_user_supplied_pk(self, mysql_conn) -> None:
        """create() works with user-supplied non-auto PK."""
        item = await Item.objects.create(
            mysql_conn,
            code="ABC123",
            description="Test item",
        )

        assert item.code == "ABC123"
        assert item.description == "Test item"

    async def test_created_instance_is_readable(self, mysql_conn) -> None:
        """Instance created via create() is readable via get()."""
        created = await Product.objects.create(
            mysql_conn,
            name="Gadget",
            price=Decimal("29.99"),
        )

        fetched = await Product.objects.get(mysql_conn, id=created.id)
        assert fetched.id == created.id
        assert fetched.name == "Gadget"
        assert fetched.price == Decimal("29.99")


class TestMysqlBulkCreateReturningFalse:
    """Test MySQL bulk_create(returning=False)."""

    async def test_bulk_create_returning_false_returns_count(self, mysql_conn) -> None:
        """bulk_create(returning=False) returns inserted row count."""
        count = await Product.objects.bulk_create(
            mysql_conn,
            [
                Product(name="Bulk A", price=Decimal("5.00")),
                Product(name="Bulk B", price=Decimal("6.00")),
                Product(name="Bulk C", price=Decimal("7.00")),
            ],
            returning=False,
        )

        assert count == 3

    async def test_bulk_create_returning_false_inserts_data(self, mysql_conn) -> None:
        """bulk_create(returning=False) actually inserts the rows."""
        await Product.objects.bulk_create(
            mysql_conn,
            [
                Product(name="Test 1", price=Decimal("1.00")),
                Product(name="Test 2", price=Decimal("2.00")),
            ],
            returning=False,
        )

        all_products = await Product.objects.all(mysql_conn)
        assert len(all_products) >= 2
        assert any(p.name == "Test 1" for p in all_products)
        assert any(p.name == "Test 2" for p in all_products)


class TestMysqlBulkCreateReturningTrue:
    """Test MySQL bulk_create(returning=True) raises clear error."""

    async def test_bulk_create_returning_true_raises_clear_error(self, mysql_conn) -> None:
        """bulk_create(returning=True) raises FerrumConfigError with clear message."""
        with pytest.raises(FerrumConfigError) as exc_info:
            await Product.objects.bulk_create(
                mysql_conn,
                [
                    Product(name="Should fail", price=Decimal("1.00")),
                ],
                returning=True,
            )

        error_msg = str(exc_info.value)
        # Message says "returning=True" (capital T), so after .lower() it becomes "returning=true"
        assert "bulk_create" in error_msg.lower()
        assert "returning" in error_msg.lower()
        assert "not supported" in error_msg.lower()
        assert "mysql" in error_msg.lower()
