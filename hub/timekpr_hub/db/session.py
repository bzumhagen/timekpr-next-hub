"""Async SQLAlchemy engine/session factory.

Single uvicorn worker against Postgres is the intended deployment (PLAN
"Tech stack" — "Single uvicorn worker = single writer = no contention"), so
there's no pooling exotica here: a plain async engine with a small pool.
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://timekpr_hub:timekpr_hub@localhost:5432/timekpr_hub",
)

engine = create_async_engine(DATABASE_URL, pool_pre_ping=True, pool_size=5, max_overflow=5)

SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session
