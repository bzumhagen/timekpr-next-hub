"""Unit coverage for agent/timekpr_hub_agent/policy_push.py::_apply_policy_push
and its helpers -- the agent-side half of pushing a policy to a device.
Exercises every field `PolicyPayload` can carry,
plus the two correctness issues the advanced editor would otherwise expose
silently: the ALLOWED_WEEKDAYS/LIMITS_PER_WEEKDAYS positional-index bug, and
the empty-allowed_hours lockout trap.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st
from timekpr_hub_agent.policy_push import _apply_policy_push, _project_daily_limits_to_allowed_days

from tests.e2e.harness import FakeEnforcer
from tests.fakes.fake_timekpr import FakeTimekprDaemon

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


# --- the push diff. timekpr notifies the user on every setter it is
# handed, changed or not, so each redundant write is a "policy changed"
# popup and the call count is what these assert on.


def test_re_pushing_an_unchanged_policy_touches_dbus_zero_times():
    enforcer = _fresh_enforcer()
    assert _apply_policy_push(enforcer, "kiddo", _FULL_POLICY) is True
    first_pass = list(enforcer.write_calls)
    assert first_pass, "a fresh device must push everything"

    enforcer.write_calls.clear()
    assert _apply_policy_push(enforcer, "kiddo", _FULL_POLICY) is True
    assert enforcer.write_calls == []

    enforcer.write_calls.clear()
    assert _apply_policy_push(enforcer, "kiddo", _FULL_POLICY) is True
    assert enforcer.write_calls == []


def test_changing_one_field_re_pushes_only_that_field():
    enforcer = _fresh_enforcer()
    assert _apply_policy_push(enforcer, "kiddo", _FULL_POLICY) is True

    enforcer.write_calls.clear()
    changed = dict(_FULL_POLICY, weekly_limit_s=_FULL_POLICY["weekly_limit_s"] + 600)
    assert _apply_policy_push(enforcer, "kiddo", changed) is True
    assert enforcer.write_calls == ["set_time_limit_for_week"]
    assert enforcer._weekly_limits["kiddo"] == 1600


def test_one_changed_hours_day_does_not_re_push_the_other_six():
    from timekpr_hub_core.allowed_hours import TimeInterval, unrestricted

    unrestricted_wire = _wire_hours(unrestricted())
    standing = {str(day): unrestricted_wire for day in range(1, 8)}
    enforcer = _fresh_enforcer()
    assert _apply_policy_push(enforcer, "kiddo", dict(_FULL_POLICY, allowed_hours=standing)) is True

    enforcer.write_calls.clear()
    narrowed = dict(standing, **{"3": _wire_hours([TimeInterval(9 * 60, 17 * 60)])})
    assert _apply_policy_push(enforcer, "kiddo", dict(_FULL_POLICY, allowed_hours=narrowed)) is True
    assert enforcer.write_calls == ["set_allowed_hours"]
    assert enforcer._allowed_hours["kiddo"]["3"] == _dbus_hours(9, 17)


def test_changing_allowed_weekdays_always_re_pushes_the_daily_limits():
    """A readback taken under the old weekday set is positional within it
    (see `_project_daily_limits_to_allowed_days`), so it can compare equal
    to the new projection while no longer being aligned to it. If
    setAllowedDays ran, setTimeLimitForDays must run."""
    enforcer = _fresh_enforcer()
    # Equal projections either side, so only the coupling rule can force
    # the second write.
    same_limit_everywhere = dict(_FULL_POLICY, daily_limits_s=[3600] * 7)
    assert _apply_policy_push(enforcer, "kiddo", same_limit_everywhere) is True

    enforcer.write_calls.clear()
    fewer_days = dict(same_limit_everywhere, allowed_weekdays=["1", "3", "4", "5", "6"])
    assert _apply_policy_push(enforcer, "kiddo", fewer_days) is True
    assert "set_allowed_days" in enforcer.write_calls
    assert "set_time_limit_for_days" in enforcer.write_calls
    assert enforcer._daily_limits["kiddo"] == [3600] * 5
    assert enforcer._allowed_weekdays["kiddo"] == ["1", "3", "4", "5", "6"]


def test_an_unreadable_device_config_pushes_everything():
    """The diff may only suppress a write it knows is redundant, so an
    unreadable config has to fail toward applying."""
    enforcer = _fresh_enforcer()
    assert _apply_policy_push(enforcer, "kiddo", _FULL_POLICY) is True
    full_push = list(enforcer.write_calls)

    enforcer._read_fails = True
    enforcer.write_calls.clear()
    assert _apply_policy_push(enforcer, "kiddo", _FULL_POLICY) is True
    assert enforcer.write_calls == full_push


def test_a_persistently_failing_step_stops_dragging_the_others_with_it():
    """`_apply_policy_push` is all-or-nothing, so one failing step keeps
    the caller from advancing `policy_revision_applied` and the hub
    resending forever. The retry must be only the field that is wrong."""
    enforcer = _fresh_enforcer()
    failed_calls = []

    def _always_fails(username: str, activities) -> bool:
        failed_calls.append(activities)
        enforcer.write_calls.append("set_playtime_activities")
        return False

    enforcer.set_playtime_activities = _always_fails

    assert _apply_policy_push(enforcer, "kiddo", _FULL_POLICY) is False
    first_pass = list(enforcer.write_calls)
    assert len(first_pass) > 1, "a fresh device still pushes every field"
    enforcer.write_calls.clear()

    # Still outstanding, because the failed write left PLAYTIME_ACTIVITIES
    # absent from the readback.
    assert _apply_policy_push(enforcer, "kiddo", _FULL_POLICY) is False
    assert enforcer.write_calls == ["set_playtime_activities"]
    assert len(failed_calls) == 2
    assert "PLAYTIME_ACTIVITIES" not in enforcer.get_applied_policy("kiddo")


def test_lockout_wake_window_change_alone_re_pushes_the_lockout_type():
    """One call carries both, so both have to match to skip it."""
    enforcer = _fresh_enforcer()
    assert _apply_policy_push(enforcer, "kiddo", _FULL_POLICY) is True

    enforcer.write_calls.clear()
    moved_wake = dict(_FULL_POLICY, wake_to="9")
    assert _apply_policy_push(enforcer, "kiddo", moved_wake) is True
    assert enforcer.write_calls == ["set_lockout_type"]
    assert enforcer._lockout["kiddo"] == ("suspendwake", "7", "9")


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
