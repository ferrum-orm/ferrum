"""Ferrum + FastAPI quickstart example.

Demonstrates the target developer experience (README.md / PRODUCT_DESIGN.md).
This file is intentionally minimal — production apps should split models,
routers, and configuration into separate modules.

NOTE: This file uses Ferrum APIs that are not yet implemented (connection layer
pending). It serves as a living specification of the target API shape.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, HTTPException

import ferrum
from ferrum.contrib.fastapi import ferrum_lifespan


# ---------------------------------------------------------------------------
# Model definition
# ---------------------------------------------------------------------------

class User(ferrum.Model):
    id: int
    email: str
    active: bool = True


# ---------------------------------------------------------------------------
# Application lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    database_url = os.environ["DATABASE_URL"]
    async with ferrum_lifespan(database_url=database_url):
        yield


app = FastAPI(title="Ferrum Quickstart", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/users", response_model=list[User])
async def list_users() -> list[User]:
    return await User.objects.filter(active=True).order_by("-id").all()  # type: ignore[attr-defined]


@app.get("/users/{user_id}", response_model=User)
async def get_user(user_id: int) -> User:
    try:
        return await User.objects.get(id=user_id)  # type: ignore[attr-defined]
    except ferrum.FerrumNotFoundError:
        raise HTTPException(status_code=404, detail="User not found") from None
