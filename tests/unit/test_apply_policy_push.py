"""Unit coverage for agent/timekpr_hub_agent/main.py::_apply_policy_push and
its helpers -- the agent-side half of pushing a policy to a device.
Exercises every field `PolicyPayload` can carry,
plus the two correctness issues the advanced editor would otherwise expose
silently: the ALLOWED_WEEKDAYS/LIMITS_PER_WEEKDAYS positional-index bug, and
the empty-allowed_hours lockout trap.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st
from timekpr_hub_agent.fake_timekpr import FakeTimekprDaemon
from timekpr_hub_agent.main import _apply_policy_push, _project_daily_limits_to_allowed_days

from tests.e2e.harness import FakeEnforcer

_FULL_POLICY = {
    "daily_limits_s": [100, 200, 300, 400, 500, 600, 700],
    "allowed_weekdays": ["1", "3", "4", "5", "6", "7"],  # Tuesday excluded
    "weekly_limit_s": 1000,
    "monthly_limit_s": 5000,
    "track_inactive": True,
    "hide_tray_icon": False,
    "lockout_type": "suspendwake",
    "wake_from": "7",
    "wake_to": "8",
    "allowed_hours": {"1": [{"hour": 8, "start_min": 0, "end_min": 60, "unaccounted": False}]},
    "playtime": {
        "enabled": True,
        "override_enabled": False,
        "unaccounted_intervals_enabled": False,
        "allowed_weekdays": ["1"],
        "daily_limits_s": [30, 0, 0, 0, 0, 0, 0],
        "activities": [{"mask": "steam", "description": "Steam games"}],
    },
}


def _fresh_enforcer() -> FakeEnforcer:
    return FakeEnforcer({"kiddo": FakeTimekprDaemon()})


def test_apply_policy_push_pushes_every_field():
    enforcer = _fresh_enforcer()
    assert _apply_policy_push(enforcer, "kiddo", _FULL_POLICY) is True

    # Issue #1: LIMITS_PER_WEEKDAYS must be projected to the allowed-days
    # subset, in the same order, with Tuesday's own limit (200) dropped --
    # not shifted onto Wednesday.
    assert enforcer._daily_limits["kiddo"] == [100, 300, 400, 500, 600, 700]
    assert enforcer._allowed_weekdays["kiddo"] == ["1", "3", "4", "5", "6", "7"]

    assert enforcer._weekly_limits["kiddo"] == 1000
    assert enforcer._monthly_limits["kiddo"] == 5000
    assert enforcer._track_inactive["kiddo"] is True
    assert enforcer._hide_tray_icon["kiddo"] is False
    assert enforcer._lockout["kiddo"] == ("suspendwake", "7", "8")
    # Hour keys MUST be strings ("8", not 8) -- timekpr's own
    # checkAndSetAllowedHours re-indexes the dict with a stringified key it
    # derives by iterating it, so an int-keyed dict raises KeyError there
    # and the push silently "fails" every tick forever with no visible
    # error (see core/timekpr_hub_core/allowed_hours.py::
    # hours_to_dbus_payload's docstring -- this shipped as a real bug once).
    assert enforcer._allowed_hours["kiddo"]["1"] == {"8": {"STARTMIN": 0, "ENDMIN": 60, "UACC": False}}

    assert enforcer._playtime_enabled["kiddo"] is True
    assert enforcer._playtime_override["kiddo"] is False
    assert enforcer._playtime_unaccounted_intervals["kiddo"] is False
    assert enforcer._playtime_allowed_weekdays["kiddo"] == ["1"]
    # PlayTime limits are projected the same way as the main daily limits.
    assert enforcer._playtime_daily_limits["kiddo"] == [30]
    assert enforcer._playtime_activities["kiddo"] == [("steam", "Steam games")]


def test_apply_policy_push_refuses_an_empty_allowed_hours_day():
    """An absent hour means 'forbidden' to timekpr -- pushing an empty list
    for any day must fail loudly rather than silently locking the user out
    of that day entirely."""
    enforcer = _fresh_enforcer()
    bad = dict(_FULL_POLICY, allowed_hours={"2": []})
    assert _apply_policy_push(enforcer, "kiddo", bad) is False


def test_apply_policy_push_rejects_wrong_daily_limit_count():
    enforcer = _fresh_enforcer()
    bad = dict(_FULL_POLICY, daily_limits_s=[100, 200, 300])
    assert _apply_policy_push(enforcer, "kiddo", bad) is False


def _wire_hours(intervals) -> list[dict]:
    from timekpr_hub_core.allowed_hours import intervals_to_hours

    return [
        {"hour": r.hour, "start_min": r.start_min, "end_min": r.end_min, "unaccounted": r.unaccounted}
        for r in intervals_to_hours(intervals)
    ]


def _dbus_hours(start_hour: int, end_hour_exclusive: int) -> dict:
    """The `setAllowedHours` payload shape for a contiguous whole-hour
    range, matching what `hours_to_dbus_payload` produces."""
    hours = range(start_hour, end_hour_exclusive)
    return {str(h): {"STARTMIN": 0, "ENDMIN": 60, "UACC": False} for h in hours}


def test_a_materialized_hours_payload_pushes_all_seven_weekdays():
    """The one-day-hours-override feature depends on the hub always sending
    a fully materialized `allowed_hours` (all 7 keys) -- see
    `timekpr_hub_core.effective_policy.materialize_allowed_hours`'s
    docstring for why an absent key can never be reverted. This asserts the
    agent applies exactly that: 7 `setAllowedHours` calls, string keys
    throughout, one per weekday."""
    from timekpr_hub_core.allowed_hours import unrestricted

    materialized = {str(day): _wire_hours(unrestricted()) for day in range(1, 8)}
    enforcer = _fresh_enforcer()
    policy = dict(_FULL_POLICY, allowed_hours=materialized)
    assert _apply_policy_push(enforcer, "kiddo", policy) is True
    assert set(enforcer._allowed_hours["kiddo"].keys()) == {"1", "2", "3", "4", "5", "6", "7"}
    for payload in enforcer._allowed_hours["kiddo"].values():
        assert payload == _dbus_hours(0, 24)


def test_pushing_an_hours_override_then_the_standing_payload_reverts_it():
    """The single most important test for the one-day allowed-hours
    override: proves the un-push. Weekday 3's standing hours are
    09:00-17:00; a one-day override widens it to "any time" for a push
    (simulating today's `/sync`); a LATER push (simulating the day after,
    once the override no longer applies) sends the standing payload again
    and must land back on 09:00-17:00 -- not leave weekday 3 stuck open."""
    from timekpr_hub_core.allowed_hours import TimeInterval, unrestricted

    standing_window = _wire_hours([TimeInterval(9 * 60, 17 * 60)])
    unrestricted_wire = _wire_hours(unrestricted())
    standing_hours = {str(day): (standing_window if day == 3 else unrestricted_wire) for day in range(1, 8)}
    overridden_hours = dict(standing_hours, **{"3": unrestricted_wire})

    enforcer = _fresh_enforcer()

    # Tick 1: the override is in effect for weekday 3.
    override_policy = dict(_FULL_POLICY, allowed_hours=overridden_hours)
    assert _apply_policy_push(enforcer, "kiddo", override_policy) is True
    assert enforcer._allowed_hours["kiddo"]["3"] == _dbus_hours(0, 24)

    # Tick 2: the standing payload is pushed again -- weekday 3 must be
    # back to 09:00-17:00, not left at "any time".
    standing_policy = dict(_FULL_POLICY, allowed_hours=standing_hours)
    assert _apply_policy_push(enforcer, "kiddo", standing_policy) is True
    assert enforcer._allowed_hours["kiddo"]["3"] == _dbus_hours(9, 17)


@given(
    daily_limits=st.lists(st.integers(min_value=0, max_value=86400), min_size=7, max_size=7),
    allowed_days=st.lists(st.sampled_from(["1", "2", "3", "4", "5", "6", "7"]), min_size=1, unique=True),
)
@settings(max_examples=200)
def test_projection_gives_each_allowed_day_its_own_limit(daily_limits, allowed_days):
    """Property version of issue #1: whichever subset of days is allowed,
    each projected position must equal that specific day's own configured
    limit (index = ISO weekday - 1), never a neighboring day's."""
    projected = _project_daily_limits_to_allowed_days(daily_limits, allowed_days)
    assert projected == [daily_limits[int(day) - 1] for day in allowed_days]
