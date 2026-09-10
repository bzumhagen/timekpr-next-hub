"""Layer 4 verification (PLAN "Verification"): FastAPI app against a real
Postgres, exercising the enroll -> approve -> sync flow end-to-end.

Requires a running Postgres reachable via $TEST_DATABASE_URL with
migrations applied (same as test_aggregate_postgres.py) -- see README.md
"Testing". Marked `db`; see that file's docstring for what runs it.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from timekpr_hub.app import app
from timekpr_hub.db.session import get_session

from tests.dbutil import TEST_DATABASE_URL, require_db

pytestmark = pytest.mark.db

# One engine per test *session* (not per app import), created lazily inside
# whatever event loop pytest-asyncio is actually running -- and injected via
# FastAPI's dependency_overrides rather than relying on the app's own
# module-level `db.session.engine`, which is bound once at import time to
# whichever loop touches it first. Mixing that global engine across
# pytest-asyncio's per-test event loops is what caused
# "attached to a different loop" asyncpg errors during initial development
# of this test file; overriding the dependency sidesteps the whole problem
# and is the standard pattern for testing FastAPI + async SQLAlchemy apps.
_test_engine = None
_test_sessionmaker = None


def _get_test_sessionmaker():
    global _test_engine, _test_sessionmaker
    if _test_engine is None:
        _test_engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
        _test_sessionmaker = async_sessionmaker(_test_engine, expire_on_commit=False, class_=AsyncSession)
    return _test_sessionmaker


async def _override_get_session():
    session_factory = _get_test_sessionmaker()
    async with session_factory() as session:
        yield session


@pytest_asyncio.fixture
async def client():
    await require_db()

    session_factory = _get_test_sessionmaker()
    async with session_factory() as session:
        await session.execute(
            text(
                "TRUNCATE users, devices, activity_intervals, usage_counters, "
                "policies, enrollment_codes, grants CASCADE"
            )
        )
        await session.commit()

    app.dependency_overrides[get_session] = _override_get_session
    try:
        async with LifespanManager(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
                yield ac
    finally:
        app.dependency_overrides.pop(get_session, None)


async def _seed_user(username: str = "alpha") -> None:
    session_factory = _get_test_sessionmaker()
    async with session_factory() as session:
        await session.execute(
            text("INSERT INTO users (id, canonical_username, display_name) VALUES (:id, :u, :d)"),
            {"id": uuid.uuid4(), "u": username, "d": username.title()},
        )
        await session.commit()


@pytest.mark.asyncio
async def test_healthz(client):
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_enroll_requires_valid_code(client):
    resp = await client.post(
        "/api/v1/enroll",
        json={
            "enrollment_code": "NOPE1234",
            "hostname": "h",
            "machine_id": "m",
            "os": "linux",
            "tz": "UTC",
            "agent_version": "0.1.0",
            "local_users": [],
        },
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_enrollment_code_is_single_use(client):
    code_resp = await client.post("/api/v1/enrollment-codes")
    code = code_resp.json()["code"]

    body = {
        "enrollment_code": code,
        "hostname": "kitchen-pc",
        "machine_id": "m1",
        "os": "linux",
        "tz": "UTC",
        "agent_version": "0.1.0",
        "local_users": [],
    }
    first = await client.post("/api/v1/enroll", json=body)
    assert first.status_code == 201

    second = await client.post("/api/v1/enroll", json=body)
    assert second.status_code == 409


@pytest.mark.asyncio
async def test_sync_requires_device_token(client):
    resp = await client.post(
        "/api/v1/sync",
        json={
            "agent_time": "2026-09-09T00:00:00+00:00",
            "tz": "UTC",
            "ntp_synced": True,
            "agent_version": "0.1.0",
            "users": [],
        },
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_sync_rejects_revoked_device_immediately(client):
    """PLAN pitfall: 'Never fail open on an auth error.'"""
    code_resp = await client.post("/api/v1/enrollment-codes")
    code = code_resp.json()["code"]
    enroll_resp = await client.post(
        "/api/v1/enroll",
        json={
            "enrollment_code": code,
            "hostname": "h",
            "machine_id": "m",
            "os": "linux",
            "tz": "UTC",
            "agent_version": "0.1.0",
            "local_users": [],
        },
    )
    device_id = enroll_resp.json()["device_id"]
    token = enroll_resp.json()["device_token"]

    # revoke it directly (no admin endpoint yet in Phase 1 -- direct DB write)
    session_factory = _get_test_sessionmaker()
    async with session_factory() as session:
        await session.execute(text("UPDATE devices SET status = 'revoked' WHERE id = :id"), {"id": device_id})
        await session.commit()

    resp = await client.post(
        "/api/v1/sync",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "agent_time": "2026-09-09T00:00:00+00:00",
            "tz": "UTC",
            "ntp_synced": True,
            "agent_version": "0.1.0",
            "users": [],
        },
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_full_enroll_approve_sync_flow_two_devices_wallclock_burn_once(client):
    """End-to-end acceptance test (PLAN "Layer 5"): two devices, one pooled
    user, wall-clock 'burn once' accounting -- via the real HTTP API against
    real Postgres, not just the pure-Python simulation."""
    await _seed_user("alpha")

    async def enroll_and_approve(hostname: str) -> tuple[str, str]:
        code = (await client.post("/api/v1/enrollment-codes")).json()["code"]
        resp = await client.post(
            "/api/v1/enroll",
            json={
                "enrollment_code": code,
                "hostname": hostname,
                "machine_id": hostname,
                "os": "linux",
                "tz": "UTC",
                "agent_version": "0.1.0",
                "local_users": ["alpha"],
            },
        )
        assert resp.status_code == 201
        device_id, token = resp.json()["device_id"], resp.json()["device_token"]
        approve_resp = await client.post(f"/api/v1/devices/{device_id}/approve")
        assert approve_resp.status_code == 200
        return device_id, token

    _, token_a = await enroll_and_approve("kitchen-pc")
    _, token_b = await enroll_and_approve("loft-laptop")

    async def sync(token: str, cumulative_s: int, start: str, end: str) -> dict:
        resp = await client.post(
            "/api/v1/sync",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "agent_time": "2026-09-09T14:00:00+00:00",
                "tz": "UTC",
                "ntp_synced": True,
                "agent_version": "0.1.0",
                "users": [
                    {
                        "username": "alpha",
                        "day": "2026-09-09",
                        "cumulative_spent_s": cumulative_s,
                        "active_span": {"start": start, "end": end, "burned_s": cumulative_s},
                        "observed": {
                            "balance_s": cumulative_s,
                            "spent_day_s": cumulative_s,
                            "limit_today_s": 3600,
                            "logged_in": True,
                            "active": True,
                        },
                        "local_grant_s": 0,
                        "policy_version_applied": 0,
                    }
                ],
            },
        )
        assert resp.status_code == 200
        return resp.json()["users"][0]

    # Both devices active for the SAME 30-minute window -> burn once, not 60.
    result_a = await sync(token_a, 1800, "2026-09-09T14:00:00+00:00", "2026-09-09T14:30:00+00:00")
    result_b = await sync(token_b, 1800, "2026-09-09T14:00:00+00:00", "2026-09-09T14:30:00+00:00")

    assert result_a["global_spent_s"] == 1800
    assert result_b["global_spent_s"] == 1800  # same union, not 3600
