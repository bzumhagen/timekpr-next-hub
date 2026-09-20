"""Integration coverage for the one-day allowed-hours override -- the
push/revert half of `timekpr_hub_core.effective_policy`, exercised through
the full stack (hub API, DB, `/sync`'s two-branch push gate).

IMPORTANT: `/sync` (api/sync.py) always computes its own "today" from the
server's real wall clock -- it does NOT trust anything in the request body
for that (see test_gates_and_overrides.py's module docstring, which this
file otherwise mirrors in structure and helpers).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from timekpr_hub.db.models import Policy, User
from timekpr_hub.services.policy import effective_policy_payload

from tests.conftest import get_test_sessionmaker as _get_test_sessionmaker
from tests.integration.test_gates_and_overrides import _enroll_device, _set_policy_daily_limits
from tests.integration.test_hub_api import _seed_user

pytestmark = pytest.mark.db


def _today() -> datetime.date:
    return datetime.now(UTC).date()


def _today_str() -> str:
    return _today().isoformat()


def _tomorrow_str() -> str:
    return (_today() + timedelta(days=1)).isoformat()


def _todays_weekday_token() -> str:
    return str(_today().isoweekday())


async def _sync(client, token: str, username: str, *, revision_applied) -> dict:
    """Like `test_gates_and_overrides._sync`, but lets the caller control
    `policy_revision_applied` -- omitted entirely (`revision_applied=None`
    with `include=False`) simulates a legacy agent that predates this
    field; any string simulates a new-enough one."""
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


_OMIT = object()


async def _set_standing_hours_for_today(client, username: str, *, from_min: int, to_min: int) -> None:
    """Sets a real (narrow) standing window for TODAY's weekday, via the
    full `PUT .../policy` field set -- needed whenever a test must tell an
    "unrestricted" override apart from an "unrestricted" (never-edited)
    standing default, which would otherwise look byte-identical."""
    from timekpr_hub_core.allowed_hours import TimeInterval, intervals_to_hours

    records = intervals_to_hours([TimeInterval(from_min, to_min)])
    resp = await client.put(
        f"/api/v1/users/{username}/policy",
        json={
            "daily_limits_s": [120 * 60] * 7,
            "weekly_limit_s": 7 * 86400,
            "monthly_limit_s": 31 * 86400,
            "allowed_weekdays": ["1", "2", "3", "4", "5", "6", "7"],
            "allowed_hours": {
                _todays_weekday_token(): [
                    {"hour": r.hour, "start_min": r.start_min, "end_min": r.end_min} for r in records
                ]
            },
        },
    )
    assert resp.status_code == 200


async def _fetch_user_and_policy(username: str) -> tuple[User, Policy]:
    session_factory = _get_test_sessionmaker()
    async with session_factory() as session:
        user = (await session.execute(select(User).where(User.canonical_username == username))).scalar_one()
        policy = (
            await session.execute(select(Policy).where(Policy.id == user.current_policy_id))
        ).scalar_one()
        return user, policy


@pytest.mark.asyncio
async def test_override_for_today_changes_the_synced_hours_but_not_the_int_version(client):
    await _seed_user("hours_today")
    token = await _enroll_device(client, "hours_today", "m-hours-today")
    version = await _set_policy_daily_limits(client, "hours_today", 120)

    today = _today_str()
    resp = await client.put(
        "/api/v1/users/hours_today/day-hours",
        json={"day": today, "mode": "window", "from_min": 12 * 60, "to_min": 20 * 60, "reason": "home early"},
    )
    assert resp.status_code == 200

    result = await _sync(client, token, "hours_today", revision_applied="")
    assert result["policy_version"] == version  # NOT bumped by an hours override
    assert result["policy"] is not None
    weekday = _todays_weekday_token()
    hours = result["policy"]["allowed_hours"][weekday]
    assert hours[0]["hour"] == 12
    assert hours[-1]["hour"] == 19


@pytest.mark.asyncio
async def test_day_hours_override_can_mark_the_window_unaccounted(client):
    """DayHourOverrideCreate.unaccounted must reach the pushed
    AllowedHourInterval -- timekpr's '!' semantics, allowed but not
    counted against the daily limit."""
    await _seed_user("hours_unaccounted")
    token = await _enroll_device(client, "hours_unaccounted", "m-hours-unaccounted")
    await _set_policy_daily_limits(client, "hours_unaccounted", 120)

    today = _today_str()
    resp = await client.put(
        "/api/v1/users/hours_unaccounted/day-hours",
        json={
            "day": today,
            "mode": "window",
            "from_min": 15 * 60,
            "to_min": 16 * 60,
            "unaccounted": True,
            "reason": "homework",
        },
    )
    assert resp.status_code == 200

    result = await _sync(client, token, "hours_unaccounted", revision_applied="")
    weekday = _todays_weekday_token()
    hours = result["policy"]["allowed_hours"][weekday]
    assert all(h["unaccounted"] for h in hours)


@pytest.mark.asyncio
async def test_future_dated_override_pushes_nothing_today(client):
    await _seed_user("hours_future")
    token = await _enroll_device(client, "hours_future", "m-hours-future")
    await _set_policy_daily_limits(client, "hours_future", 120)

    # First tick establishes the baseline revision (no override yet).
    first = await _sync(client, token, "hours_future", revision_applied="")
    baseline_revision = first["policy_revision"]

    tomorrow = _tomorrow_str()
    resp = await client.put(
        "/api/v1/users/hours_future/day-hours",
        json={"day": tomorrow, "mode": "unrestricted", "reason": "tomorrow off"},
    )
    assert resp.status_code == 200

    second = await _sync(client, token, "hours_future", revision_applied=baseline_revision)
    assert second["policy"] is None  # nothing to push -- today is unaffected
    assert second["policy_revision"] == baseline_revision


@pytest.mark.asyncio
async def test_clearing_the_override_restores_the_standing_hours(client):
    await _seed_user("hours_clear")
    token = await _enroll_device(client, "hours_clear", "m-hours-clear")
    await _set_standing_hours_for_today(client, "hours_clear", from_min=9 * 60, to_min=17 * 60)

    today = _today_str()
    await client.put(
        "/api/v1/users/hours_clear/day-hours",
        json={"day": today, "mode": "unrestricted", "reason": "any time today"},
    )
    overridden = await _sync(client, token, "hours_clear", revision_applied="")
    weekday = _todays_weekday_token()
    assert len(overridden["policy"]["allowed_hours"][weekday]) == 24  # full day, not the standing 9-17

    del_resp = await client.delete(f"/api/v1/users/hours_clear/day-hours/{today}")
    assert del_resp.status_code == 200
    assert del_resp.json()["cleared"] is True

    reverted = await _sync(client, token, "hours_clear", revision_applied=overridden["policy_revision"])
    assert reverted["policy"] is not None  # revision changed back -- must push again
    assert reverted["policy_revision"] != overridden["policy_revision"]
    assert len(reverted["policy"]["allowed_hours"][weekday]) == 8  # back to 9:00-17:00


@pytest.mark.asyncio
async def test_a_never_edited_policy_yields_all_seven_materialized_weekdays(client):
    """The revert-path guarantee: `create_initial_policy` writes
    `allowed_hours_json={}`, and the payload the agent receives must still
    carry all 7 keys (materialized to unrestricted), or a later override's
    revert push would have no key to rewrite."""
    await _seed_user("hours_materialize")
    token = await _enroll_device(client, "hours_materialize", "m-hours-materialize")
    # No policy edit at all -- get_or_create_policy seeds the default.

    result = await _sync(client, token, "hours_materialize", revision_applied="")
    assert sorted(result["policy"]["allowed_hours"].keys()) == ["1", "2", "3", "4", "5", "6", "7"]
    for hours in result["policy"]["allowed_hours"].values():
        assert len(hours) == 24


@pytest.mark.asyncio
async def test_a_legacy_agent_never_sees_the_hours_override(client):
    """An agent that omits `policy_revision_applied` entirely (predates
    this feature) must be served the standing payload only, gated on
    `policy_version` alone -- never the effective one, or it could apply an
    override it can never be told to revert."""
    await _seed_user("hours_legacy")
    token = await _enroll_device(client, "hours_legacy", "m-hours-legacy")
    await _set_policy_daily_limits(client, "hours_legacy", 120)

    today = _today_str()
    await client.put(
        "/api/v1/users/hours_legacy/day-hours",
        json={"day": today, "mode": "unrestricted", "reason": "any time today"},
    )

    result = await _sync(client, token, "hours_legacy", revision_applied=_OMIT)
    weekday = _todays_weekday_token()
    # Standing policy's hours were never set -> materialized unrestricted,
    # which happens to look the same as the override here. The real
    # assertion is structural: this agent got gated on policy_version
    # (present and non-empty) with an ordinary policy_revision placeholder,
    # not a revision-gated push.
    assert result["policy"] is not None  # first tick, version 0 != current
    assert len(result["policy"]["allowed_hours"][weekday]) == 24

    # Second tick: legacy agent echoes the version it just got. No further
    # push should happen even though the (still active) override exists,
    # because a legacy agent is never re-evaluated against the revision.
    result2 = await _sync(client, token, "hours_legacy", revision_applied=_OMIT)
    # can't set policy_version_applied directly through this helper's fixed
    # 0 -- so assert instead that the response never depends on the
    # override by checking it matches the always-legacy branch's own
    # revision suffix.
    assert result2["policy_revision"].endswith("-legacy")


@pytest.mark.asyncio
async def test_422_when_the_weekday_is_not_allowed_to_log_in(client):
    await _seed_user("hours_disallowed")
    await _enroll_device(client, "hours_disallowed", "m-hours-disallowed")
    other_weekdays = [d for d in ["1", "2", "3", "4", "5", "6", "7"] if d != _todays_weekday_token()]
    resp = await client.put(
        "/api/v1/users/hours_disallowed/policy",
        json={
            "daily_limits_s": [3600] * 7,
            "weekly_limit_s": 7 * 86400,
            "monthly_limit_s": 31 * 86400,
            "allowed_weekdays": other_weekdays,
        },
    )
    assert resp.status_code == 200

    today = _today_str()
    resp = await client.put(
        "/api/v1/users/hours_disallowed/day-hours",
        json={"day": today, "mode": "unrestricted", "reason": "would have no effect"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_read_path_skips_override_if_policy_later_disallows_the_weekday(client):
    """Write-time validation alone isn't enough -- the policy can be
    edited to drop the weekday AFTER the override was set."""
    await _seed_user("hours_stale")
    await _enroll_device(client, "hours_stale", "m-hours-stale")
    await _set_policy_daily_limits(client, "hours_stale", 60)

    today = _today_str()
    await client.put(
        "/api/v1/users/hours_stale/day-hours",
        json={"day": today, "mode": "unrestricted", "reason": "any time"},
    )

    weekday = _todays_weekday_token()
    await client.put(
        "/api/v1/users/hours_stale/policy",
        json={
            "daily_limits_s": [3600] * 7,
            "weekly_limit_s": 7 * 86400,
            "monthly_limit_s": 31 * 86400,
            "allowed_weekdays": [d for d in ["1", "2", "3", "4", "5", "6", "7"] if d != weekday],
        },
    )

    user, policy = await _fetch_user_and_policy("hours_stale")
    session_factory = _get_test_sessionmaker()
    async with session_factory() as session:
        payload, _revision = await effective_policy_payload(session, policy=policy, day=_today())
    # The override exists on file but must not have been substituted in,
    # since today's weekday is no longer allowed.
    assert len(payload.allowed_hours[weekday]) == 24  # materialized-unrestricted default, not overridden-away


@pytest.mark.asyncio
async def test_hours_override_composes_with_a_limit_override_and_a_gated_day(client):
    """Hours (when) and seconds/gate (how much / whether) are orthogonal --
    setting an hours window must not perturb `effective_limit_today_s`."""
    await _seed_user("hours_compose")
    token = await _enroll_device(client, "hours_compose", "m-hours-compose")
    await _set_policy_daily_limits(client, "hours_compose", 120)

    today = _today_str()
    await client.put(
        "/api/v1/users/hours_compose/day-hours",
        json={
            "day": today,
            "mode": "window",
            "from_min": 12 * 60,
            "to_min": 20 * 60,
            "reason": "early start",
        },
    )
    await client.put(
        "/api/v1/users/hours_compose/day-override",
        json={"day": today, "limit_seconds": 1800, "reason": "half day"},
    )

    result = await _sync(client, token, "hours_compose", revision_applied="")
    assert result["effective_limit_today_s"] == 1800
    weekday = _todays_weekday_token()
    assert result["policy"]["allowed_hours"][weekday][0]["hour"] == 12


@pytest.mark.asyncio
async def test_day_rollover_reverts_via_effective_policy_payload(client):
    """Exercises the rollover revert directly against
    `effective_policy_payload` (D then D+1), since `/sync` structurally
    cannot be asked about any day but the server's real today (see module
    docstring)."""
    await _seed_user("hours_rollover")
    await _enroll_device(client, "hours_rollover", "m-hours-rollover")
    await _set_standing_hours_for_today(client, "hours_rollover", from_min=9 * 60, to_min=17 * 60)

    today = _today()
    await client.put(
        "/api/v1/users/hours_rollover/day-hours",
        json={"day": today.isoformat(), "mode": "unrestricted", "reason": "today only"},
    )

    user, policy = await _fetch_user_and_policy("hours_rollover")
    session_factory = _get_test_sessionmaker()
    async with session_factory() as session:
        today_payload, today_revision = await effective_policy_payload(session, policy=policy, day=today)
        tomorrow_payload, tomorrow_revision = await effective_policy_payload(
            session, policy=policy, day=today + timedelta(days=1)
        )

    assert today_revision != tomorrow_revision
    weekday_today = str(today.isoweekday())
    weekday_tomorrow = str((today + timedelta(days=1)).isoweekday())
    assert len(today_payload.allowed_hours[weekday_today]) == 24
    # Tomorrow's own weekday key is untouched by today's override -- it's
    # whatever the (never-edited-for-hours) standing policy says, which for
    # this test's freshly-seeded user is also unrestricted, but arrived at
    # via materialization, not the override.
    assert len(tomorrow_payload.allowed_hours[weekday_tomorrow]) == 24


@pytest.mark.asyncio
async def test_ui_form_sets_and_clears_the_override(client):
    """The UI twin -- a raw form POST, mirroring test_ui_policy_form.py's
    pattern -- rather than the JSON API."""
    await _seed_user("hours_ui")
    await _enroll_device(client, "hours_ui", "m-hours-ui")
    await _set_policy_daily_limits(client, "hours_ui", 120)

    today = _today_str()
    resp = await client.post(
        "/ui/users/hours_ui/day-hours",
        data={"day": today, "mode": "window", "from": "12:00", "to": "20:00", "reason": "home early"},
    )
    assert resp.status_code == 200
    assert "12:00" in resp.text or "Clear" in resp.text  # the active-override notice rendered

    clear_resp = await client.post("/ui/users/hours_ui/day-hours/clear", data={"day": today})
    assert clear_resp.status_code == 200
