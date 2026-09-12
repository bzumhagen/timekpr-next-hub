"""Integration coverage for chore gates and per-date limit overrides --
see the plan's verification section. Exercises the full stack (hub API,
DB, the `combine_limit` collapse in services/limits.py + summaries.py)
rather than just the pure `combine_limit` unit tests
(tests/unit/test_combine_limit.py).

IMPORTANT: `/sync` (api/sync.py) always computes its own "today" from the
server's real wall clock (`datetime.now(UTC)`) -- it does NOT trust
anything in the request body for that. So any assertion that needs a
*specific* day (today, tomorrow, "a different Saturday") either has to use
the real current day, or has to bypass HTTP and check the DB/service layer
directly for a manufactured date. Both patterns appear below; each test
says which it's doing and why.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from timekpr_hub.db.models import Policy, User
from timekpr_hub.services.limits import (
    base_daily_limit,
    combine_limit,
    day_override,
    grants_total,
    is_gate_released,
    is_gated_weekday,
)

from tests.conftest import get_test_sessionmaker as _get_test_sessionmaker
from tests.integration.test_hub_api import _seed_user

pytestmark = pytest.mark.db


def _today_str() -> str:
    return datetime.now(UTC).date().isoformat()


def _tomorrow_str() -> str:
    return (datetime.now(UTC).date() + timedelta(days=1)).isoformat()


def _todays_weekday_token() -> str:
    return str(datetime.now(UTC).isoweekday())


async def _enroll_device(client, username: str, machine_id: str) -> str:
    code = (await client.post("/api/v1/enrollment-codes")).json()["code"]
    resp = await client.post(
        "/api/v1/enroll",
        json={
            "enrollment_code": code,
            "hostname": "h",
            "machine_id": machine_id,
            "os": "linux",
            "tz": "UTC",
            "agent_version": "0.1.0",
            "local_users": [username],
        },
    )
    return resp.json()["device_token"]


async def _sync(client, token: str, username: str) -> dict:
    """Always reports the server's real "today" -- see the module
    docstring. `day`/`agent_time` in the request are informational only."""
    today = _today_str()
    resp = await client.post(
        "/api/v1/sync",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "agent_time": f"{today}T12:00:00+00:00",
            "tz": "UTC",
            "ntp_synced": True,
            "agent_version": "0.1.0",
            "users": [
                {
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
            ],
        },
    )
    return resp.json()["users"][0]


async def _set_policy_daily_limits(client, username: str, minutes_per_day: int) -> int:
    resp = await client.put(
        f"/api/v1/users/{username}/policy",
        json={
            "daily_limits_s": [minutes_per_day * 60] * 7,
            "weekly_limit_s": 7 * 86400,
            "monthly_limit_s": 31 * 86400,
            "allowed_weekdays": ["1", "2", "3", "4", "5", "6", "7"],
        },
    )
    return resp.json()["version"]


async def _fetch_user_and_policy(username: str) -> tuple[User, Policy]:
    session_factory = _get_test_sessionmaker()
    async with session_factory() as session:
        user = (await session.execute(select(User).where(User.canonical_username == username))).scalar_one()
        policy = (
            await session.execute(select(Policy).where(Policy.id == user.current_policy_id))
        ).scalar_one()
        return user, policy


# --------------------------------------------------------------------------
# Day overrides -- exercised through /sync for TODAY (full stack), since
# that's the only day /sync will ever actually report on.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_zero_override_zeroes_the_day(client):
    await _seed_user("override_zero")
    token = await _enroll_device(client, "override_zero", "m-override-zero")
    await _set_policy_daily_limits(client, "override_zero", 60)

    today = _today_str()
    resp = await client.put(
        "/api/v1/users/override_zero/day-override",
        json={"day": today, "limit_seconds": 0, "reason": "grounded"},
    )
    assert resp.status_code == 200

    result = await _sync(client, token, "override_zero")
    assert result["effective_limit_today_s"] == 0


@pytest.mark.asyncio
async def test_nonzero_override_sets_the_limit_exactly(client):
    await _seed_user("override_partial")
    token = await _enroll_device(client, "override_partial", "m-override-partial")
    await _set_policy_daily_limits(client, "override_partial", 120)  # 2h standing limit

    today = _today_str()
    resp = await client.put(
        "/api/v1/users/override_partial/day-override",
        json={"day": today, "limit_seconds": 1800, "reason": "half day"},
    )
    assert resp.status_code == 200

    result = await _sync(client, token, "override_partial")
    assert result["effective_limit_today_s"] == 1800


@pytest.mark.asyncio
async def test_override_survives_a_later_change_to_the_standing_limit(client):
    """The exact case that motivated DayOverride over a negative grant: a
    grant's stored second-count would silently stop cancelling the day once
    the weekday's standing limit changed; an override must not drift."""
    await _seed_user("override_drift")
    token = await _enroll_device(client, "override_drift", "m-override-drift")
    await _set_policy_daily_limits(client, "override_drift", 60)

    today = _today_str()
    await client.put(
        "/api/v1/users/override_drift/day-override",
        json={"day": today, "limit_seconds": 0, "reason": "moratorium"},
    )

    # Standing limit changes AFTER the override was set.
    await _set_policy_daily_limits(client, "override_drift", 180)

    result = await _sync(client, token, "override_drift")
    assert result["effective_limit_today_s"] == 0  # still zero, not drifted to 3h


@pytest.mark.asyncio
async def test_clearing_an_override_restores_the_standing_limit(client):
    await _seed_user("override_clear")
    token = await _enroll_device(client, "override_clear", "m-override-clear")
    await _set_policy_daily_limits(client, "override_clear", 90)

    today = _today_str()
    await client.put(
        "/api/v1/users/override_clear/day-override",
        json={"day": today, "limit_seconds": 0, "reason": "grounded"},
    )
    del_resp = await client.delete(f"/api/v1/users/override_clear/day-override/{today}")
    assert del_resp.status_code == 200
    assert del_resp.json()["cleared"] is True

    result = await _sync(client, token, "override_clear")
    assert result["effective_limit_today_s"] == 90 * 60


@pytest.mark.asyncio
async def test_grant_composes_with_an_override(client):
    """override is 'instead of' the base, grant is 'in addition to' either --
    the two must compose."""
    await _seed_user("override_plus_grant")
    token = await _enroll_device(client, "override_plus_grant", "m-override-plus-grant")
    await _set_policy_daily_limits(client, "override_plus_grant", 120)

    today = _today_str()
    await client.put(
        "/api/v1/users/override_plus_grant/day-override",
        json={"day": today, "limit_seconds": 0, "reason": "grounded"},
    )
    await client.post(
        "/api/v1/users/override_plus_grant/grants",
        json={"seconds": 900, "reason": "still get 15 min", "day": today},
    )

    result = await _sync(client, token, "override_plus_grant")
    assert result["effective_limit_today_s"] == 900  # override's 0 + the 900s grant on top


# --------------------------------------------------------------------------
# Dated grants -- "today" is verified through /sync (full stack); "a
# different day didn't leak" has to be checked directly against the DB,
# since /sync structurally cannot be asked about any day but today.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dated_grant_lands_on_the_target_day_not_today(client):
    await _seed_user("dated_grant")
    token = await _enroll_device(client, "dated_grant", "m-dated-grant")
    await _set_policy_daily_limits(client, "dated_grant", 60)

    tomorrow = _tomorrow_str()
    resp = await client.post(
        "/api/v1/users/dated_grant/grants", json={"seconds": 1800, "reason": "bonus", "day": tomorrow}
    )
    assert resp.status_code == 201
    assert resp.json()["day"] == tomorrow

    # Today must be unaffected -- verified through the real /sync path.
    today_result = await _sync(client, token, "dated_grant")
    assert today_result["effective_limit_today_s"] == 60 * 60

    # Tomorrow's grant landed where it should -- checked directly against
    # the DB, since /sync has no way to report on a day that isn't today.
    user, policy = await _fetch_user_and_policy("dated_grant")
    session_factory = _get_test_sessionmaker()
    async with session_factory() as session:
        tomorrow_date = datetime.now(UTC).date() + timedelta(days=1)
        grants_s = await grants_total(session, user_id=user.id, day=tomorrow_date)
        base_s = base_daily_limit(policy, tomorrow_date)
        limit_s = combine_limit(base_s=base_s, grants_s=grants_s, gated=False, released=False)
    assert limit_s == 60 * 60 + 1800


# --------------------------------------------------------------------------
# Chore gate -- "today" through /sync; "a different date wasn't released"
# checked directly against the DB for the same reason as above.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gated_day_reports_zero_then_the_real_limit_after_release(client):
    await _seed_user("gated_user")
    token = await _enroll_device(client, "gated_user", "m-gated")
    await _set_policy_daily_limits(client, "gated_user", 90)

    today = _today_str()
    today_weekday = _todays_weekday_token()
    settings_resp = await client.put(
        "/api/v1/users/gated_user/settings",
        json={"gated_weekdays": [today_weekday], "accounting_mode": "wallclock"},
    )
    assert settings_resp.status_code == 200

    before_release = await _sync(client, token, "gated_user")
    assert before_release["effective_limit_today_s"] == 0

    release_resp = await client.post(
        "/api/v1/users/gated_user/gate-release", json={"day": today, "note": "chores done"}
    )
    assert release_resp.status_code == 201

    after_release = await _sync(client, token, "gated_user")
    assert after_release["effective_limit_today_s"] == 90 * 60


@pytest.mark.asyncio
async def test_release_is_date_scoped_not_weekday_scoped(client):
    """Releasing TODAY must not release the same weekday next week -- the
    gate is meant to re-arm every week on its own. /sync can't report on
    next week's date, so this checks `is_gate_released` directly."""
    await _seed_user("gated_scoped")
    today = _today_str()
    today_weekday = _todays_weekday_token()

    await client.put(
        "/api/v1/users/gated_scoped/settings",
        json={"gated_weekdays": [today_weekday], "accounting_mode": "wallclock"},
    )
    await client.post("/api/v1/users/gated_scoped/gate-release", json={"day": today})

    next_week_same_weekday = datetime.now(UTC).date() + timedelta(days=7)

    session_factory = _get_test_sessionmaker()
    async with session_factory() as session:
        user = (
            await session.execute(select(User).where(User.canonical_username == "gated_scoped"))
        ).scalar_one()
        released_today = await is_gate_released(session, user_id=user.id, day=datetime.now(UTC).date())
        released_next_week = await is_gate_released(session, user_id=user.id, day=next_week_same_weekday)

    assert released_today is True
    assert released_next_week is False


@pytest.mark.asyncio
async def test_unrelease_regates_the_day(client):
    await _seed_user("gated_unrelease")
    token = await _enroll_device(client, "gated_unrelease", "m-gated-unrelease")
    await _set_policy_daily_limits(client, "gated_unrelease", 45)
    today = _today_str()
    today_weekday = _todays_weekday_token()
    await client.put(
        "/api/v1/users/gated_unrelease/settings",
        json={"gated_weekdays": [today_weekday], "accounting_mode": "wallclock"},
    )

    await client.post("/api/v1/users/gated_unrelease/gate-release", json={"day": today})
    unrelease_resp = await client.delete(f"/api/v1/users/gated_unrelease/gate-release/{today}")
    assert unrelease_resp.status_code == 200
    assert unrelease_resp.json()["unreleased"] is True

    result = await _sync(client, token, "gated_unrelease")
    assert result["effective_limit_today_s"] == 0


# --------------------------------------------------------------------------
# The regression the limits.py collapse exists to prevent: the dashboard's
# batched path, the stats page's history path, and /sync's own computation
# must all agree on the same (gated, today) number.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dashboard_stats_and_sync_agree_on_a_gated_day(client):
    await _seed_user("agree_user")
    token = await _enroll_device(client, "agree_user", "m-agree")
    await _set_policy_daily_limits(client, "agree_user", 75)

    today_weekday = _todays_weekday_token()
    await client.put(
        "/api/v1/users/agree_user/settings",
        json={"gated_weekdays": [today_weekday], "accounting_mode": "wallclock"},
    )

    sync_result = await _sync(client, token, "agree_user")
    assert sync_result["effective_limit_today_s"] == 0

    users_resp = await client.get("/api/v1/users")
    agree_row = next(u for u in users_resp.json() if u["username"] == "agree_user")
    assert agree_row["today_effective_limit_s"] == 0  # dashboard's compute_user_summaries path
    assert agree_row["gated_today"] is True
    assert agree_row["gate_released_today"] is False

    # And the same combiner, called the way summaries.py's history path
    # calls it, for today specifically.
    user, policy = await _fetch_user_and_policy("agree_user")
    session_factory = _get_test_sessionmaker()
    async with session_factory() as session:
        today_date = datetime.now(UTC).date()
        override = await day_override(session, user_id=user.id, day=today_date)
        base_s = override.limit_seconds if override else base_daily_limit(policy, today_date)
        gated = is_gated_weekday(user, today_date)
        released = await is_gate_released(session, user_id=user.id, day=today_date)
        history_style_limit = combine_limit(base_s=base_s, grants_s=0, gated=gated, released=released)

    assert history_style_limit == 0
    assert sync_result["effective_limit_today_s"] == 0
    assert agree_row["today_effective_limit_s"] == 0
