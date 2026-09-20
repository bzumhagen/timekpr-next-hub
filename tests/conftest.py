"""Shared test-session setup.

Sets `DATABASE_URL` to the test database before anything imports
`timekpr_hub` (whose db/session.py builds its engine off that env var at
import time). Most DB-backed tests override FastAPI's `get_session`
instead and never touch that module-level engine, but `tests/e2e` runs
the real app under real uvicorn with no such override -- there, whichever
env var happened to be set when `timekpr_hub.app` was first imported would
silently decide which database a live run hits. Setting it here, the one
file pytest always loads first, removes that ordering accident.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

from tests.dbutil import TEST_DATABASE_URL, all_table_names

os.environ.setdefault("DATABASE_URL", TEST_DATABASE_URL)

# --------------------------------------------------------------------------
# Shared DB-backed API client fixtures (`client` / `unauthenticated_client`).
# Live here rather than in one test module so every module gets them with
# no import -- importing a @pytest_asyncio.fixture function directly into
# another module works for pytest's own resolution, but ruff's F811 flags
# every test parameter of the same name as "redefining" it.
# --------------------------------------------------------------------------

import httpx  # noqa: E402
import pytest_asyncio  # noqa: E402
from asgi_lifespan import LifespanManager  # noqa: E402
from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402
from timekpr_hub.api.admin_auth import get_current_admin_api, get_current_admin_ui  # noqa: E402
from timekpr_hub.app import app  # noqa: E402
from timekpr_hub.db.models import Admin, Policy, User  # noqa: E402
from timekpr_hub.db.session import get_session  # noqa: E402

# One engine per test session, created lazily inside whatever event loop
# pytest-asyncio is running, injected via dependency_overrides rather than
# the app's own module-level engine (bound once to whichever loop touches
# it first) -- mixing that global engine across per-test event loops is
# what caused "attached to a different loop" asyncpg errors.
_test_engine = None
_test_sessionmaker = None


def get_test_sessionmaker():
    global _test_engine, _test_sessionmaker
    if _test_engine is None:
        _test_engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
        _test_sessionmaker = async_sessionmaker(_test_engine, expire_on_commit=False, class_=AsyncSession)
    return _test_sessionmaker


async def _override_get_session():
    session_factory = get_test_sessionmaker()
    async with session_factory() as session:
        yield session


# A stand-in admin for every test that isn't exercising the login flow
# itself -- `client` bypasses the two get_current_admin_* dependencies the
# same way it overrides get_session. Tests of the login flow use
# `unauthenticated_client` instead, which does NOT install this override.
_FAKE_ADMIN = Admin(id=uuid.uuid4(), email="test-admin@example.com", password_hash="unused-in-tests")


async def _override_get_current_admin():
    return _FAKE_ADMIN


async def _truncate_all(session_factory) -> None:
    async with session_factory() as session:
        await session.execute(text(f"TRUNCATE {all_table_names()} CASCADE"))
        await session.commit()


@pytest_asyncio.fixture
async def client():
    from tests.dbutil import require_db

    await require_db()
    await _truncate_all(get_test_sessionmaker())

    app.dependency_overrides[get_session] = _override_get_session
    app.dependency_overrides[get_current_admin_api] = _override_get_current_admin
    app.dependency_overrides[get_current_admin_ui] = _override_get_current_admin
    try:
        async with LifespanManager(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
                yield ac
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(get_current_admin_api, None)
        app.dependency_overrides.pop(get_current_admin_ui, None)


@pytest_asyncio.fixture
async def unauthenticated_client():
    """Like `client`, but leaves the real admin-auth dependencies in place
    -- for tests of the auth gate and the login/setup flow themselves."""
    from tests.dbutil import require_db

    await require_db()
    await _truncate_all(get_test_sessionmaker())

    app.dependency_overrides[get_session] = _override_get_session
    try:
        async with LifespanManager(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
                yield ac
    finally:
        app.dependency_overrides.pop(get_session, None)


# --------------------------------------------------------------------------
# Shared by test_gates_and_overrides.py and test_day_hour_overrides.py --
# both need "today" in the server's real wall clock (/sync never trusts
# the request body for it) and a user's policy row outside the API.
# --------------------------------------------------------------------------


def _today_str() -> str:
    return datetime.now(UTC).date().isoformat()


def _tomorrow_str() -> str:
    return (datetime.now(UTC).date() + timedelta(days=1)).isoformat()


def _todays_weekday_token() -> str:
    return str(datetime.now(UTC).isoweekday())


async def _fetch_user_and_policy(username: str) -> tuple[User, Policy]:
    session_factory = get_test_sessionmaker()
    async with session_factory() as session:
        user = (await session.execute(select(User).where(User.canonical_username == username))).scalar_one()
        policy = (
            await session.execute(select(Policy).where(Policy.id == user.current_policy_id))
        ).scalar_one()
        return user, policy


_OMIT = object()


async def _sync(client, token: str, username: str, *, revision_applied=_OMIT) -> dict:
    """Posts one /sync tick for `username` reporting zero activity -- for
    tests that only care what /sync echoes back (effective limits/hours),
    not the convergence math itself. `revision_applied` simulates the
    agent's `policy_revision_applied` echo; omitted (the default) simulates
    an agent that predates that field."""
    today = _today_str()
    user = {
        "username": username,
        "day": today,
        "cumulative_spent_s": 0,
        "observed": {
            "balance_s": 0,
            "spent_day_s": 0,
            "limit_today_s": 3600,
            "logged_in": False,
            "active": False,
        },
        "local_grant_s": 0,
        "policy_version_applied": 0,
    }
    if revision_applied is not _OMIT:
        user["policy_revision_applied"] = revision_applied
    resp = await client.post(
        "/api/v1/sync",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "agent_time": f"{today}T12:00:00+00:00",
            "tz": "UTC",
            "ntp_synced": True,
            "agent_version": "0.1.0",
            "users": [user],
        },
    )
    return resp.json()["users"][0]
