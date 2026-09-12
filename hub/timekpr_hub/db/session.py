"""Async SQLAlchemy engine/session factory.

Single uvicorn worker against Postgres is the intended deployment (PLAN
"Tech stack" — "Single uvicorn worker = single writer = no contention"), so
there's no pooling exotica here: a plain async engine with a small pool.

Built at import time off `settings.database_url` (a `Settings` field read
from the `DATABASE_URL` env var, not a raw `os.environ.get` -- see
`settings.py`). Import time, not inside a `lifespan` handler, is
deliberate: `tests/e2e/harness.py` imports this module's `engine` directly
and disposes/re-binds it itself to drive a real uvicorn process across
event loops, and `tests/conftest.py` relies on `DATABASE_URL` being set in
the environment *before* this module is first imported -- both would break
under a lifespan-created engine. `app.py`'s `_lifespan` disposes this same
engine on shutdown instead.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from timekpr_hub.settings import settings

engine = create_async_engine(settings.database_url, pool_pre_ping=True, pool_size=5, max_overflow=5)

SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session
