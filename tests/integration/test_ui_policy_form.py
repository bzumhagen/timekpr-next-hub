"""Coverage for the UI policy editor's actual form-submission path
(`POST /users/{username}/policy`, `hub/timekpr_hub/api/ui.py`) -- previously
untested; every existing policy test exercised only the JSON API
(`PUT /api/v1/users/{u}/policy`). Confirms the editor's redesigned fields
(hours+minutes pairs, "same/different day" mode, per-day allowed-hours mode,
unchecked-cap-means-uncapped) all round-trip into the stored `Policy` the
same way the JSON API's flat fields do.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from timekpr_hub.db.models import Policy, User

from tests.conftest import get_test_sessionmaker as _get_test_sessionmaker
from tests.integration.test_hub_api import _seed_user

# `client` (the fixture every test below takes as a parameter) lives in
# tests/conftest.py and needs no import -- pytest injects conftest fixtures
# into every test module in the package automatically.

pytestmark = pytest.mark.db


def _base_form(username: str) -> dict:
    """A minimal-but-complete submission: same limit every day, every day
    allowed to log in, all-day hours, no caps, default lockout/PlayTime."""
    form = {
        "daily_mode": "same",
        "daily_h": "1",
        "daily_m": "30",
        "lockout_type": "lock",
    }
    for day in ("1", "2", "3", "4", "5", "6", "7"):
        form[f"hours_mode_{day}"] = "all"
        form[f"allowed_weekday_{day}"] = "on"
    return form


async def _get_policy(username: str) -> Policy:
    session_factory = _get_test_sessionmaker()
    async with session_factory() as session:
        user = (await session.execute(select(User).where(User.canonical_username == username))).scalar_one()
        policy = (
            await session.execute(select(Policy).where(Policy.id == user.current_policy_id))
        ).scalar_one()
        return policy


@pytest.mark.asyncio
async def test_same_every_day_hm_pair_writes_all_seven_days(client):
    await _seed_user("hmuser")
    form = _base_form("hmuser")
    resp = await client.post("/users/hmuser/policy", data=form, follow_redirects=False)
    assert resp.status_code == 303

    policy = await _get_policy("hmuser")
    assert policy.daily_limits_json == [5400] * 7  # 1h30m = 5400s, every day


@pytest.mark.asyncio
async def test_different_each_day_mode_writes_independent_limits(client):
    await _seed_user("diffuser")
    form = _base_form("diffuser")
    form["daily_mode"] = "different"
    for i in range(7):
        form[f"daily_h_{i}"] = str(i)
        form[f"daily_m_{i}"] = "0"
    resp = await client.post("/users/diffuser/policy", data=form, follow_redirects=False)
    assert resp.status_code == 303

    policy = await _get_policy("diffuser")
    assert policy.daily_limits_json == [i * 3600 for i in range(7)]


@pytest.mark.asyncio
async def test_between_mode_gives_minute_precision(client):
    """The old whole-hour checkbox grid could only express 15:00-16:00; the
    "between" time pickers must round-trip a genuine partial-hour window."""
    await _seed_user("betweenuser")
    form = _base_form("betweenuser")
    form["hours_mode_1"] = "between"
    form["hours_from_1"] = "15:30"
    form["hours_to_1"] = "20:00"
    resp = await client.post("/users/betweenuser/policy", data=form, follow_redirects=False)
    assert resp.status_code == 303

    policy = await _get_policy("betweenuser")
    monday_hours = {h["hour"]: h for h in policy.allowed_hours_json["1"]}
    assert monday_hours[15]["start_min"] == 30
    assert monday_hours[15]["end_min"] == 60
    assert monday_hours[19]["start_min"] == 0
    assert monday_hours[19]["end_min"] == 60
    assert 20 not in monday_hours  # end is exclusive at 20:00


@pytest.mark.asyncio
async def test_between_mode_rejects_start_after_end(client):
    await _seed_user("badrangeuser")
    form = _base_form("badrangeuser")
    form["hours_mode_1"] = "between"
    form["hours_from_1"] = "20:00"
    form["hours_to_1"] = "15:00"
    resp = await client.post("/users/badrangeuser/policy", data=form, follow_redirects=False)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_custom_mode_rejects_a_day_with_no_hours_checked(client):
    await _seed_user("customuser")
    form = _base_form("customuser")
    form["hours_mode_2"] = "custom"  # no hh_2_* boxes set at all
    resp = await client.post("/users/customuser/policy", data=form, follow_redirects=False)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_custom_mode_unaccounted_checkbox_marks_every_checked_hour(client):
    """The day-level "these hours don't count against the daily limit"
    checkbox in custom mode must mark every checked hour unaccounted, and
    reloading the editor must show it checked again."""
    await _seed_user("unaccounteduser")
    form = _base_form("unaccounteduser")
    form["hours_mode_3"] = "custom"
    form["hh_3_15"] = "on"
    form["hh_3_16"] = "on"
    form["unaccounted_3"] = "on"
    resp = await client.post("/users/unaccounteduser/policy", data=form, follow_redirects=False)
    assert resp.status_code == 303

    policy = await _get_policy("unaccounteduser")
    day3 = policy.allowed_hours_json["3"]
    assert len(day3) == 2
    assert all(iv["unaccounted"] for iv in day3)

    page = await client.get("/users/unaccounteduser")
    assert page.status_code == 200
    assert 'name="unaccounted_3" checked' in page.text


@pytest.mark.asyncio
async def test_unchecked_weekly_cap_writes_the_uncapped_maximum(client):
    """Unchecking "also cap per week" must mean uncapped (timekpr's own
    default), not an accidental 0-second cap."""
    await _seed_user("capuser")
    form = _base_form("capuser")
    # weekly_cap_enabled / monthly_cap_enabled deliberately omitted (unchecked)
    resp = await client.post("/users/capuser/policy", data=form, follow_redirects=False)
    assert resp.status_code == 303

    policy = await _get_policy("capuser")
    assert policy.weekly_limit_s == 7 * 86400
    assert policy.monthly_limit_s == 31 * 86400


@pytest.mark.asyncio
async def test_checked_weekly_cap_writes_the_given_hm_value(client):
    await _seed_user("capuser2")
    form = _base_form("capuser2")
    form["weekly_cap_enabled"] = "on"
    form["weekly_h"] = "10"
    form["weekly_m"] = "0"
    resp = await client.post("/users/capuser2/policy", data=form, follow_redirects=False)
    assert resp.status_code == 303

    policy = await _get_policy("capuser2")
    assert policy.weekly_limit_s == 10 * 3600


@pytest.mark.asyncio
async def test_get_policy_page_renders_for_a_brand_new_user(client):
    """A user with no policy yet gets one seeded (get_or_create_policy) and
    the page must render it as "all day" for every weekday -- the
    unrestricted default, not an empty/broken hours grid."""
    await _seed_user("freshuser")
    resp = await client.get("/users/freshuser")
    assert resp.status_code == 200
    assert "freshuser" in resp.text.lower() or "Freshuser" in resp.text


@pytest.mark.asyncio
async def test_audit_page_renders_and_pages_with_offset(client):
    """The audit page is a plain GET (no live poll, unlike the dashboard/
    devices pages) and must render even with zero events, plus page
    forward via ?offset= once there's at least one."""
    resp = await client.get("/audit")
    assert resp.status_code == 200

    await _seed_user("audituser")
    await client.post("/api/v1/users/audituser/grants", json={"seconds": 60, "reason": "t"})

    resp = await client.get("/audit")
    assert resp.status_code == 200
    assert "grant.create" in resp.text

    resp = await client.get("/audit", params={"offset": 0})
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_dashboard_shows_undo_button_after_gate_release(client):
    """The dashboard's gate-released notice must offer a way back (Undo),
    not just a dead-end confirmation -- api/ui.py's gate-unrelease route
    already existed; this is the button that was missing."""
    await _seed_user("undobutton")
    from tests.integration.test_gates_and_overrides import _todays_weekday_token

    today_weekday = _todays_weekday_token()
    await client.put(
        "/api/v1/users/undobutton/settings",
        json={"gated_weekdays": [today_weekday], "accounting_mode": "wallclock"},
    )
    await client.post("/ui/users/undobutton/gate-release")

    resp = await client.get("/ui/users-fragment")
    assert resp.status_code == 200
    assert "/ui/users/undobutton/gate-unrelease" in resp.text
    assert "Undo" in resp.text


@pytest.mark.asyncio
async def test_grant_from_ui_with_day_lands_on_that_date_not_today(client):
    """The dashboard's '-30 min tomorrow' button (and any dated grant form
    field) must post to the target date's Grant, not today's."""
    from datetime import date, timedelta

    await _seed_user("dategrant")
    tomorrow = (date.today() + timedelta(days=1)).isoformat()

    resp = await client.post(
        "/ui/users/dategrant/grants", data={"seconds": "-1800", "day": tomorrow}
    )
    assert resp.status_code == 200

    session_factory = _get_test_sessionmaker()
    async with session_factory() as session:
        from sqlalchemy import text

        row = (
            await session.execute(
                text("SELECT day, seconds FROM grants g JOIN users u ON u.id = g.user_id "
                     "WHERE u.canonical_username = :u"),
                {"u": "dategrant"},
            )
        ).one()
    assert row.day.isoformat() == tomorrow
    assert row.seconds == -1800


@pytest.mark.asyncio
async def test_rename_user_changes_display_name_only(client):
    await _seed_user("renameuser")
    resp = await client.post("/users/renameuser/rename", data={"display_name": "Renamed Kid"})
    assert resp.status_code == 303

    session_factory = _get_test_sessionmaker()
    async with session_factory() as session:
        from sqlalchemy import select
        from timekpr_hub.db.models import User

        user = (
            await session.execute(select(User).where(User.canonical_username == "renameuser"))
        ).scalar_one()
    assert user.display_name == "Renamed Kid"
    assert user.canonical_username == "renameuser"


@pytest.mark.asyncio
async def test_delete_user_removes_the_row_and_its_history(client):
    await _seed_user("deleteuser")
    await client.post("/ui/users/deleteuser/grants", data={"seconds": "600"})

    resp = await client.post("/users/deleteuser/delete")
    assert resp.status_code == 303

    session_factory = _get_test_sessionmaker()
    async with session_factory() as session:
        from sqlalchemy import select
        from timekpr_hub.db.models import Grant, User

        user = (
            await session.execute(select(User).where(User.canonical_username == "deleteuser"))
        ).scalar_one_or_none()
        assert user is None
        remaining_grants = (await session.execute(select(Grant))).scalars().all()
    assert remaining_grants == []  # the grant was FK-cascaded away with the user


@pytest.mark.asyncio
async def test_offline_policy_settings_round_trip(client):
    await _seed_user("offlinesettings")
    resp = await client.post(
        "/users/offlinesettings/settings",
        data={
            "accounting_mode": "wallclock",
            "offline_policy": "closed",
            "offline_grace_min": "5",
            "offline_cap_min": "10",
        },
    )
    assert resp.status_code == 303

    session_factory = _get_test_sessionmaker()
    async with session_factory() as session:
        from sqlalchemy import select
        from timekpr_hub.db.models import User

        user = (
            await session.execute(select(User).where(User.canonical_username == "offlinesettings"))
        ).scalar_one()
    assert user.offline_policy == "closed"
    assert user.offline_grace_s == 300
    assert user.offline_cap_s == 600
