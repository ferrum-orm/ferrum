"""Ferrum + FastAPI quickstart example.

Demonstrates the target developer experience (README.md / PRODUCT_DESIGN.md).
This file is intentionally minimal — production apps should split models,
routers, and configuration into separate modules.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException

import ferrum
from ferrum.connection import Connection
from ferrum.contrib.fastapi import ferrum_lifespan, get_ferrum_conn

FerrumConn = Annotated[Connection, Depends(get_ferrum_conn)]


# ---------------------------------------------------------------------------
# Model definition
# ---------------------------------------------------------------------------


class User(ferrum.Model):
    model_config = ferrum.ModelConfig(table="users")

    id: int
    email: str
    active: bool = True


# ---------------------------------------------------------------------------
# Application lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    async with ferrum_lifespan() as conn:
        app.state.ferrum_conn = conn
        yield


app = FastAPI(title="Ferrum Quickstart", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/users", response_model=list[User])
async def list_users(conn: FerrumConn) -> list[User]:
    return await User.objects.filter(active=True).order_by("-id").all(conn)


@app.get("/users/{user_id}", response_model=User)
async def get_user(user_id: int, conn: FerrumConn) -> User:
    try:
        return await User.objects.get(conn, id=user_id)
    except ferrum.FerrumNotFoundError:
        raise HTTPException(status_code=404, detail="User not found") from None
