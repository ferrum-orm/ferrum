"""Models for the comprehensive driver exercise suite.

These models exercise:
- Various column types (int, text, bool, decimal, date/timestamp, UUID, JSON)
- NULL handling
- Unique constraints
- Foreign keys and relationships
- Composite primary keys (where supported)
- Index creation
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from ferrum import Field, ForeignKey, ManyToMany, Model, OneToOne


class Author(Model):
    """Author with various common column types."""

    id: int
    name: Annotated[str, Field(db_index=True, max_length=100)]
    email: Annotated[str, Field(unique=True, max_length=200)]
    bio: str | None = None
    is_active: bool = True
    rating: Annotated[Decimal | None, Field(max_digits=5, decimal_places=2)] = None
    birth_date: date | None = None
    created_at: Annotated[datetime | None, Field(generated=True)] = None  # DB-generated
    updated_at: datetime | None = None
    metadata_json: dict | None = None

    class Meta:
        table = "authors"


class Publisher(Model):
    """Publisher with UUID primary key."""

    id: Annotated[UUID, Field(primary_key=True, uuid_generate="v4")]
    name: Annotated[str, Field(unique=True, max_length=150)]
    country: str | None = None
    founded_year: int | None = None

    class Meta:
        table = "publishers"


class Book(Model):
    """Book with foreign keys to Author and Publisher."""

    id: int
    title: Annotated[str, Field(db_index=True, max_length=200)]
    isbn: Annotated[str | None, Field(unique=True, max_length=20)] = None
    page_count: int | None = None
    price: Annotated[Decimal | None, Field(max_digits=10, decimal_places=2)] = None
    published_date: date | None = None
    is_available: bool = True
    summary: str | None = None
    tags: list | None = None  # JSONB list where supported, else NULL

    author_id: Annotated[int, ForeignKey("authors.id")]
    publisher_id: Annotated[UUID | None, ForeignKey("publishers.id")] = None

    class Meta:
        table = "books"


class Review(Model):
    """Review with composite primary key (where supported)."""

    book_id: Annotated[int, ForeignKey("books.id")]
    user_id: int
    rating: int  # 1-5
    comment: str | None = None
    reviewed_at: datetime | None = None

    class Meta:
        table = "reviews"
        pk_fields = ("book_id", "user_id")


class BookAuthorLink(Model):
    """Many-to-many link table for books and authors (for testing M2M)."""

    book_id: Annotated[int, ForeignKey("books.id")]
    author_id: Annotated[int, ForeignKey("authors.id")]

    class Meta:
        table = "book_author_links"
        pk_fields = ("book_id", "author_id")
