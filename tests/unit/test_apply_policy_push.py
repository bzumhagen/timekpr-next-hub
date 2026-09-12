"""Unit coverage for agent/timekpr_hub_agent/main.py::_apply_policy_push and
its helpers -- the agent-side half of "extend the agent to push everything"
(see the plan's Phase C). Exercises every field `PolicyPayload` can carry,
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
    assert enforcer._allowed_hours["kiddo"]["1"] == {8: {"STARTMIN": 0, "ENDMIN": 60, "UACC": False}}

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
