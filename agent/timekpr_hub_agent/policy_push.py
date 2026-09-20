"""Pushing a hub `PolicyPayload` to the local timekpr config over DBUS."""

from __future__ import annotations

import logging
from collections.abc import Callable

from timekpr_hub_core.allowed_hours import HourRecord, hours_to_dbus_payload

from timekpr_hub_agent.enforcer import TimekprEnforcer

log = logging.getLogger("timekpr_hub_agent")

# Not imported from timekpr_hub_core.models.WEEKDAY_TOKENS: the agent
# deliberately never imports that module (see agent/pyproject.toml -- it
# pulls in pydantic, which nothing here needs). A tuple, not a list, since
# this is handed straight to callers below.
_ALL_WEEKDAYS: tuple[str, ...] = ("1", "2", "3", "4", "5", "6", "7")


def _project_daily_limits_to_allowed_days(daily_limits: list[int], allowed_weekdays: list[str]) -> list[int]:
    """timekpr indexes LIMITS_PER_WEEKDAYS *positionally within
    ALLOWED_WEEKDAYS*, not by weekday number
    (server/user/userdata.py:265-270, server/config/configprocessor.py:
    107-113 -- both truncate to the shorter of the two lists). The hub
    stores `daily_limits_s` day-keyed (index 0=Mon..6=Sun); pushing it
    verbatim alongside a non-full `allowed_weekdays` would silently hand
    each allowed day the *wrong* day's limit (e.g. disabling Tuesday shifts
    every later day's limit back by one). Project the day-keyed array down
    to exactly the allowed days, in the same order `setAllowedDays` was
    given, so index i of both lists refers to the same weekday."""
    return [daily_limits[int(day) - 1] for day in allowed_weekdays]


def _apply_policy_push(enforcer: TimekprEnforcer, username: str, policy: dict) -> bool:
    """Apply a hub policy payload to the local timekpr config -- every field
    `PolicyPayload` carries, not just daily/weekly/monthly limits and
    allowed weekdays. All-or-nothing: the caller only advances
    `policy_version_applied` when every write below succeeds, so a partial
    failure retries whole on the next tick rather than leaving the user
    half-configured (unlike timekpr's own admin GUI, which applies fields
    one DBUS call at a time and stops on the first failure)."""
    daily_limits = [int(x) for x in policy["daily_limits_s"]]
    if len(daily_limits) != 7:
        log.error(
            "%s: policy has %d daily limits, timekpr requires 7 -- not applying", username, len(daily_limits)
        )
        return False

    ok = True

    def _step(label: str, success: bool) -> bool:
        # Named per-call logging is the whole point: the old bare `ok &=
        # call(...)` chain gave a single aggregate True/False with no way
        # to tell, from the agent's own log, which of the ~10 DBUS calls in
        # a push actually failed -- exactly the gap that made a real,
        # previously-shipped bug (allowed_hours pushed with int keys
        # instead of str, see hours_to_dbus_payload's docstring) invisible
        # from the logs alone: weekday limits kept "succeeding" (retried
        # every tick, harmlessly) while hours silently never applied, and
        # nothing in the log said so.
        nonlocal ok
        if not success:
            log.warning("%s: policy push step failed: %s", username, label)
            ok = False
        return success

    allowed_weekdays = policy.get("allowed_weekdays") or list(_ALL_WEEKDAYS)
    _step("setAllowedDays", enforcer.set_allowed_days(username, allowed_weekdays))
    _step(
        "setTimeLimitForDays",
        enforcer.set_time_limit_for_days(
            username, _project_daily_limits_to_allowed_days(daily_limits, allowed_weekdays)
        ),
    )
    _step("setTimeLimitForWeek", enforcer.set_time_limit_for_week(username, int(policy["weekly_limit_s"])))
    _step("setTimeLimitForMonth", enforcer.set_time_limit_for_month(username, int(policy["monthly_limit_s"])))
    _step(
        "setTrackInactive", enforcer.set_track_inactive(username, bool(policy.get("track_inactive", False)))
    )
    _step("setHideTrayIcon", enforcer.set_hide_tray_icon(username, bool(policy.get("hide_tray_icon", False))))
    _step(
        "setLockoutType",
        enforcer.set_lockout_type(
            username,
            policy.get("lockout_type") or "lock",
            policy.get("wake_from") or "",
            policy.get("wake_to") or "",
        ),
    )
    _apply_allowed_hours(enforcer, username, policy.get("allowed_hours") or {}, _step)
    _apply_playtime(enforcer, username, policy.get("playtime") or {}, _step)
    return ok


def _apply_allowed_hours(
    enforcer: TimekprEnforcer, username: str, allowed_hours: dict, step: Callable[[str, bool], bool]
) -> None:
    """Push each weekday's `AllowedHourInterval` list. A day *absent* from
    `allowed_hours` is left untouched here rather than pushed as empty --
    see `set_allowed_hours`'s own refusal of an empty dict, and
    `core/timekpr_hub_core/allowed_hours.py::unrestricted()` for how the hub
    itself represents "no restriction" (an explicit all-24-hours entry, not
    a missing key). Each day is logged individually via `step` (not just an
    aggregate "allowed_hours failed") -- a single bad day (e.g. one with an
    unaccounted flag or overlapping records timekpr's own config rejects)
    should be diagnosable without guessing which of the 7 it was."""
    for day, intervals in allowed_hours.items():
        records = [
            HourRecord(
                hour=int(iv["hour"]),
                start_min=int(iv["start_min"]),
                end_min=int(iv["end_min"]),
                unaccounted=bool(iv.get("unaccounted", False)),
            )
            for iv in intervals
        ]
        payload = hours_to_dbus_payload(records)
        step(f"setAllowedHours(day={day})", enforcer.set_allowed_hours(username, str(day), payload))


def _apply_playtime(
    enforcer: TimekprEnforcer, username: str, playtime: dict, step: Callable[[str, bool], bool]
) -> None:
    if not playtime:
        return
    step("setPlayTimeEnabled", enforcer.set_playtime_enabled(username, bool(playtime.get("enabled", False))))
    step(
        "setPlayTimeLimitOverride",
        enforcer.set_playtime_limit_override(username, bool(playtime.get("override_enabled", False))),
    )
    step(
        "setPlayTimeUnaccountedIntervalsEnabled",
        enforcer.set_playtime_unaccounted_intervals_enabled(
            username, bool(playtime.get("unaccounted_intervals_enabled", True))
        ),
    )
    pt_weekdays = playtime.get("allowed_weekdays") or list(_ALL_WEEKDAYS)
    step("setPlayTimeAllowedDays", enforcer.set_playtime_allowed_days(username, pt_weekdays))
    pt_daily_limits = [int(x) for x in (playtime.get("daily_limits_s") or [0] * 7)]
    step(
        "setPlayTimeLimitsForDays",
        enforcer.set_playtime_limits_for_days(
            username, _project_daily_limits_to_allowed_days(pt_daily_limits, pt_weekdays)
        ),
    )
    activities = [(a["mask"], a.get("description", "")) for a in (playtime.get("activities") or [])]
    step("setPlayTimeActivities", enforcer.set_playtime_activities(username, activities))
