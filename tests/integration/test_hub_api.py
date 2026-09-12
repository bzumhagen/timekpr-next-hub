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
from timekpr_hub.api.parent_auth import get_current_parent_api, get_current_parent_ui
from timekpr_hub.app import app
from timekpr_hub.db.models import Parent
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


# A stand-in parent for every test that isn't specifically exercising the
# login flow itself -- most of this file predates parent auth and is
# testing enroll/sync/device/grant behavior that has nothing to do with it,
# so `client` bypasses the two get_current_parent_* dependencies the same
# way it overrides get_session. `test_login_flow_*` and
# `test_parent_and_ui_routes_require_login` below use `unauthenticated_client`
# instead, which does NOT install this override.
_FAKE_PARENT = Parent(id=uuid.uuid4(), email="test-parent@example.com", password_hash="unused-in-tests")


async def _override_get_current_parent():
    return _FAKE_PARENT


@pytest_asyncio.fixture
async def client():
    await require_db()

    session_factory = _get_test_sessionmaker()
    async with session_factory() as session:
        await session.execute(
            text(
                "TRUNCATE users, devices, activity_intervals, usage_counters, "
                "policies, enrollment_codes, grants, parents, parent_sessions CASCADE"
            )
        )
        await session.commit()

    app.dependency_overrides[get_session] = _override_get_session
    app.dependency_overrides[get_current_parent_api] = _override_get_current_parent
    app.dependency_overrides[get_current_parent_ui] = _override_get_current_parent
    try:
        async with LifespanManager(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
                yield ac
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(get_current_parent_api, None)
        app.dependency_overrides.pop(get_current_parent_ui, None)


@pytest_asyncio.fixture
async def unauthenticated_client():
    """Like `client`, but leaves the real parent-auth dependencies in place
    -- for tests of the auth gate and the login/setup flow themselves."""
    await require_db()

    session_factory = _get_test_sessionmaker()
    async with session_factory() as session:
        await session.execute(
            text(
                "TRUNCATE users, devices, activity_intervals, usage_counters, "
                "policies, enrollment_codes, grants, parents, parent_sessions CASCADE"
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
async def test_enroll_provisions_a_new_user_when_none_exists(client):
    """No `_seed_user` here -- enrollment itself is the provisioning path
    now, not a prerequisite manual INSERT."""
    code = (await client.post("/api/v1/enrollment-codes")).json()["code"]
    resp = await client.post(
        "/api/v1/enroll",
        json={
            "enrollment_code": code,
            "hostname": "h",
            "machine_id": "m",
            "os": "linux",
            "tz": "UTC",
            "agent_version": "0.1.0",
            "local_users": ["brandnew"],
        },
    )
    assert resp.status_code == 201

    session_factory = _get_test_sessionmaker()
    async with session_factory() as session:
        row = (
            await session.execute(
                text("SELECT canonical_username FROM users WHERE canonical_username = 'brandnew'")
            )
        ).scalar_one_or_none()
    assert row == "brandnew"


@pytest.mark.asyncio
async def test_enroll_merges_into_an_existing_user_of_the_same_username(client):
    """A second device reporting a username that already exists on the hub
    (e.g. the same account on another machine) should alias into the same
    User row rather than erroring or creating a duplicate."""
    await _seed_user("alpha")

    code = (await client.post("/api/v1/enrollment-codes")).json()["code"]
    resp = await client.post(
        "/api/v1/enroll",
        json={
            "enrollment_code": code,
            "hostname": "second-pc",
            "machine_id": "m2",
            "os": "linux",
            "tz": "UTC",
            "agent_version": "0.1.0",
            "local_users": ["alpha"],
        },
    )
    assert resp.status_code == 201
    device_id = resp.json()["device_id"]

    session_factory = _get_test_sessionmaker()
    async with session_factory() as session:
        count = (
            await session.execute(text("SELECT count(*) FROM users WHERE canonical_username = 'alpha'"))
        ).scalar_one()
        alias_user_id = (
            await session.execute(
                text("SELECT user_id FROM user_aliases WHERE device_id = :d AND local_username = 'alpha'"),
                {"d": device_id},
            )
        ).scalar_one_or_none()
        user_id = (
            await session.execute(text("SELECT id FROM users WHERE canonical_username = 'alpha'"))
        ).scalar_one()
    assert count == 1
    assert alias_user_id == user_id


@pytest.mark.asyncio
async def test_enrolled_device_is_immediately_active_and_can_sync(client):
    """Phase 2 "code implies approval": a parent-minted enrollment code is
    itself the approval -- there's no separate un-authenticated approval
    step to actually gate anything, and the old 'pending' default just
    added a step that could sync anyway (auth.py only rejected 'revoked')."""
    code = (await client.post("/api/v1/enrollment-codes")).json()["code"]
    enroll_resp = await client.post(
        "/api/v1/enroll",
        json={
            "enrollment_code": code,
            "hostname": "h",
            "machine_id": "m",
            "os": "linux",
            "tz": "UTC",
            "agent_version": "0.1.0",
            "local_users": ["carol"],
        },
    )
    assert enroll_resp.status_code == 201
    token = enroll_resp.json()["device_token"]

    sync_resp = await client.post(
        "/api/v1/sync",
        json={
            "agent_time": "2026-09-09T00:00:00+00:00",
            "tz": "UTC",
            "ntp_synced": True,
            "agent_version": "0.1.0",
            "users": [],
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert sync_resp.status_code == 200


@pytest.mark.asyncio
async def test_enroll_response_includes_hub_tz(client):
    code = (await client.post("/api/v1/enrollment-codes")).json()["code"]
    resp = await client.post(
        "/api/v1/enroll",
        json={
            "enrollment_code": code,
            "hostname": "h",
            "machine_id": "m",
            "os": "linux",
            "tz": "UTC",
            "agent_version": "0.1.0",
            "local_users": ["dave"],
        },
    )
    assert resp.status_code == 201
    assert resp.json()["hub_tz"] == "UTC"  # settings.hub_tz default, echoed back
    assert resp.json()["new_users"] == ["dave"]
    assert "dave" in resp.json()["policies"]


@pytest.mark.asyncio
async def test_enroll_seeds_policy_from_device_snapshot_for_a_new_user(client):
    """Phase 5a: a brand-new hub user's policy should start from what the
    enrolling device already has configured, not always the 1h/day
    placeholder."""
    code = (await client.post("/api/v1/enrollment-codes")).json()["code"]
    resp = await client.post(
        "/api/v1/enroll",
        json={
            "enrollment_code": code,
            "hostname": "h",
            "machine_id": "m",
            "os": "linux",
            "tz": "UTC",
            "agent_version": "0.1.0",
            "local_users": ["erin"],
            "local_policies": {
                "erin": {
                    "daily_limits_s": [7200] * 7,
                    "weekly_limit_s": 50400,
                    "monthly_limit_s": 216000,
                    "allowed_weekdays": ["1", "2", "3", "4", "5", "6", "7"],
                }
            },
        },
    )
    assert resp.status_code == 201
    policy = resp.json()["policies"]["erin"]
    assert policy["daily_limits_s"] == [7200] * 7
    assert policy["weekly_limit_s"] == 50400


@pytest.mark.asyncio
async def test_enroll_does_not_reseed_policy_for_an_existing_user(client):
    """A second device enrolling the same (already-known) username must not
    overwrite that user's existing policy with its own local snapshot --
    only a brand-new user gets seeded."""
    await _seed_user("frank")

    code = (await client.post("/api/v1/enrollment-codes")).json()["code"]
    resp = await client.post(
        "/api/v1/enroll",
        json={
            "enrollment_code": code,
            "hostname": "h2",
            "machine_id": "m2",
            "os": "linux",
            "tz": "UTC",
            "agent_version": "0.1.0",
            "local_users": ["frank"],
            "local_policies": {
                "frank": {
                    "daily_limits_s": [7200] * 7,
                    "weekly_limit_s": 50400,
                    "monthly_limit_s": 216000,
                    "allowed_weekdays": ["1", "2", "3", "4", "5", "6", "7"],
                }
            },
        },
    )
    assert resp.status_code == 201
    assert resp.json()["new_users"] == []
    # _seed_user leaves current_policy_id unset, so this hits the
    # create_initial_policy(session, user.id) default-policy fallback --
    # 1h/day, not the 7200s snapshot the device reported.
    assert resp.json()["policies"]["frank"]["daily_limits_s"] == [3600] * 7


@pytest.mark.asyncio
async def test_ui_enrollment_code_snippet_uses_the_real_flag_names(client):
    """Regression test: the UI used to print `--hub` (not a real flag) and
    omit the required `--users`, so copy-pasting it into a terminal failed."""
    resp = await client.post("/ui/enrollment-codes")
    assert resp.status_code == 200
    assert "--hub-url" in resp.text
    assert "--hub " not in resp.text


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
                        "active_spans": [{"start": start, "end": end, "burned_s": cumulative_s}],
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


@pytest.mark.asyncio
async def test_reenroll_with_known_machine_id_rebinds_instead_of_duplicating(client):
    """The reported bug: uninstalling and reinstalling the agent (same
    machine, same /etc/machine-id) used to enroll as a brand-new device
    every time. It should now rebind to the existing device row, rotating
    its token but keeping its id and history."""
    await _seed_user("gina")

    code1 = (await client.post("/api/v1/enrollment-codes")).json()["code"]
    first = await client.post(
        "/api/v1/enroll",
        json={
            "enrollment_code": code1,
            "hostname": "gina-laptop",
            "machine_id": "fixed-machine-id",
            "os": "linux",
            "tz": "UTC",
            "agent_version": "0.1.0",
            "local_users": ["gina"],
        },
    )
    assert first.status_code == 201
    first_device_id = first.json()["device_id"]
    first_token = first.json()["device_token"]
    assert first.json()["rebound"] is False

    code2 = (await client.post("/api/v1/enrollment-codes")).json()["code"]
    second = await client.post(
        "/api/v1/enroll",
        json={
            "enrollment_code": code2,
            "hostname": "gina-laptop",
            "machine_id": "fixed-machine-id",
            "os": "linux",
            "tz": "UTC",
            "agent_version": "0.2.0",
            "local_users": ["gina"],
        },
    )
    assert second.status_code == 201
    assert second.json()["rebound"] is True
    assert second.json()["device_id"] == first_device_id
    assert second.json()["previously_enrolled_at"] is not None
    # Token was rotated -- the old one no longer works.
    second_token = second.json()["device_token"]
    assert second_token != first_token

    devices = (await client.get("/api/v1/devices")).json()
    assert len(devices) == 1  # not two rows for the same machine

    old_token_sync = await client.post(
        "/api/v1/sync",
        headers={"Authorization": f"Bearer {first_token}"},
        json={
            "agent_time": "2026-09-09T00:00:00+00:00",
            "tz": "UTC",
            "ntp_synced": True,
            "agent_version": "0.1.0",
            "users": [],
        },
    )
    assert old_token_sync.status_code == 401

    new_token_sync = await client.post(
        "/api/v1/sync",
        headers={"Authorization": f"Bearer {second_token}"},
        json={
            "agent_time": "2026-09-09T00:00:00+00:00",
            "tz": "UTC",
            "ntp_synced": True,
            "agent_version": "0.2.0",
            "users": [],
        },
    )
    assert new_token_sync.status_code == 200


@pytest.mark.asyncio
async def test_revoked_device_can_be_reenrolled_as_a_genuinely_new_row(client):
    """A parent who explicitly revokes a device (rather than it just being
    reinstalled) should get a fresh device row on the next enroll with that
    machine_id -- the partial unique index only covers non-revoked rows."""
    await _seed_user("hank")

    code1 = (await client.post("/api/v1/enrollment-codes")).json()["code"]
    first = await client.post(
        "/api/v1/enroll",
        json={
            "enrollment_code": code1,
            "hostname": "hank-pc",
            "machine_id": "hank-machine",
            "os": "linux",
            "tz": "UTC",
            "agent_version": "0.1.0",
            "local_users": ["hank"],
        },
    )
    device_id = first.json()["device_id"]
    revoke_resp = await client.post(f"/api/v1/devices/{device_id}/revoke")
    assert revoke_resp.status_code == 200
    assert revoke_resp.json()["status"] == "revoked"

    code2 = (await client.post("/api/v1/enrollment-codes")).json()["code"]
    second = await client.post(
        "/api/v1/enroll",
        json={
            "enrollment_code": code2,
            "hostname": "hank-pc",
            "machine_id": "hank-machine",
            "os": "linux",
            "tz": "UTC",
            "agent_version": "0.1.0",
            "local_users": ["hank"],
        },
    )
    assert second.status_code == 201
    assert second.json()["rebound"] is False
    assert second.json()["device_id"] != device_id


@pytest.mark.asyncio
async def test_policy_update_bumps_version_and_agent_receives_it_on_next_sync(client):
    """PUT /users/{u}/policy is the only way to change a limit through the
    hub (as opposed to an additive grant) -- confirm it creates a new policy
    version and that the very next /sync carries the new payload down."""
    await _seed_user("iris")
    code = (await client.post("/api/v1/enrollment-codes")).json()["code"]
    enroll_resp = await client.post(
        "/api/v1/enroll",
        json={
            "enrollment_code": code,
            "hostname": "iris-pc",
            "machine_id": "iris-machine",
            "os": "linux",
            "tz": "UTC",
            "agent_version": "0.1.0",
            "local_users": ["iris"],
        },
    )
    token = enroll_resp.json()["device_token"]
    initial_version = enroll_resp.json()["policies"]["iris"]["version"]

    update_resp = await client.put(
        "/api/v1/users/iris/policy",
        json={
            "daily_limits_s": [5400] * 7,
            "weekly_limit_s": 37800,
            "monthly_limit_s": 162000,
            "allowed_weekdays": ["1", "2", "3", "4", "5", "6", "7"],
        },
    )
    assert update_resp.status_code == 200
    assert update_resp.json()["version"] == initial_version + 1
    assert update_resp.json()["daily_limits_s"] == [5400] * 7

    sync_resp = await client.post(
        "/api/v1/sync",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "agent_time": "2026-09-09T00:00:00+00:00",
            "tz": "UTC",
            "ntp_synced": True,
            "agent_version": "0.1.0",
            "users": [
                {
                    "username": "iris",
                    "day": "2026-09-09",
                    "cumulative_spent_s": 0,
                    "observed": {
                        "balance_s": 0,
                        "spent_day_s": 0,
                        "limit_today_s": 3600,
                        "logged_in": False,
                        "active": False,
                    },
                    "local_grant_s": 0,
                    "policy_version_applied": initial_version,  # stale -- hasn't seen the update yet
                }
            ],
        },
    )
    assert sync_resp.status_code == 200
    resp_user = sync_resp.json()["users"][0]
    assert resp_user["policy_version"] == initial_version + 1
    assert resp_user["policy"] is not None
    assert resp_user["policy"]["daily_limits_s"] == [5400] * 7
    assert resp_user["effective_limit_today_s"] == 5400


@pytest.mark.asyncio
async def test_policy_update_rejects_out_of_range_daily_limit(client):
    await _seed_user("jax")
    resp = await client.put(
        "/api/v1/users/jax/policy",
        json={
            "daily_limits_s": [999999] + [3600] * 6,  # over 86400
            "weekly_limit_s": 25200,
            "monthly_limit_s": 108000,
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_parent_and_ui_routes_require_login(unauthenticated_client):
    """Every parent/UI route 401s (JSON) or redirects to /login (HTML)
    without a valid session -- the fix for the "everything is open on the
    LAN" finding."""
    c = unauthenticated_client
    api_resp = await c.get("/api/v1/users")
    assert api_resp.status_code == 401

    ui_resp = await c.get("/ui/users-fragment", follow_redirects=False)
    assert ui_resp.status_code in (302, 303, 307)
    assert ui_resp.headers["location"] == "/login"


@pytest.mark.asyncio
async def test_login_flow_setup_then_login_then_logout(unauthenticated_client):
    c = unauthenticated_client

    # No parent exists yet -- /setup is reachable, /login bounces to it.
    login_before_setup = await c.get("/login", follow_redirects=False)
    assert login_before_setup.status_code == 303
    assert login_before_setup.headers["location"] == "/setup"

    setup_resp = await c.post(
        "/setup", data={"email": "parent@example.com", "password": "correcthorsebattery"}
    )
    assert setup_resp.status_code == 303
    assert setup_resp.headers["location"] == "/"
    assert "tkh_session" in setup_resp.cookies

    # /setup is now locked -- a second account can't be created this way.
    setup_again = await c.get("/setup")
    assert setup_again.status_code == 404

    # The session cookie httpx just captured authenticates subsequent calls.
    authed_resp = await c.get("/api/v1/users")
    assert authed_resp.status_code == 200

    logout_resp = await c.post("/logout")
    assert logout_resp.status_code == 303
    assert logout_resp.headers["location"] == "/login"

    # Cookie is gone/invalidated -- back to 401.
    after_logout = await c.get("/api/v1/users")
    assert after_logout.status_code == 401

    # And logging back in with the right password works.
    login_resp = await c.post(
        "/login", data={"email": "parent@example.com", "password": "correcthorsebattery"}
    )
    assert login_resp.status_code == 303
    assert login_resp.headers["location"] == "/"
    assert (await c.get("/api/v1/users")).status_code == 200


@pytest.mark.asyncio
async def test_login_with_wrong_password_is_rejected(unauthenticated_client):
    c = unauthenticated_client
    await c.post("/setup", data={"email": "parent2@example.com", "password": "correcthorsebattery"})
    await c.post("/logout")

    resp = await c.post("/login", data={"email": "parent2@example.com", "password": "wrong-password"})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login?error=1"
    assert (await c.get("/api/v1/users")).status_code == 401
