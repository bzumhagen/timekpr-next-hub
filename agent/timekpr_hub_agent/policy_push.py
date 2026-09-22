"""Pushing a hub `PolicyPayload` to the local timekpr config over DBUS."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from timekpr_hub_core.allowed_hours import HourRecord, hours_to_dbus_payload

from timekpr_hub_agent.enforcer import TimekprEnforcer

log = logging.getLogger("timekpr_hub_agent")

# Not imported from timekpr_hub_core.models.WEEKDAY_TOKENS: the agent
# deliberately never imports that module (see agent/pyproject.toml -- it
# pulls in pydantic, which nothing here needs). A tuple, not a list, since
# this is handed straight to callers below.
_ALL_WEEKDAYS: tuple[str, ...] = ("1", "2", "3", "4", "5", "6", "7")


class _Unknown:
    """Marks a current value as unreadable. Not None, which is itself a
    legitimate value for an absent payload field."""

    def __repr__(self) -> str:  # pragma: no cover
        return "<unknown>"


_UNKNOWN = _Unknown()


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


class _Push:
    """One policy push, skipping the setters whose value already matches.

    Every timekpr admin setter ends in
    `adjustLimitsFromConfig(pSilent=False)`
    (server/interface/dbus/daemon.py), notifying the user whether or not
    the value changed -- so each redundant write in a ~20-call push is a
    "policy changed" popup with nothing behind it.

    A skipped step counts as success, so one field the local timekpr
    rejects can't hold back the revision the caller echoes to the hub (and
    with it, force every other field to re-push each tick).
    """

    def __init__(self, username: str, applied: dict[str, Any] | None) -> None:
        self.username = username
        self.applied = applied if applied is not None else {}
        self.readable = applied is not None
        self.ok = True
        self.applied_count = 0
        self.skipped_count = 0

    def current(self, key: str) -> Any:
        """The device's current value for `key`, or `_UNKNOWN` when the
        readback failed or didn't carry it -- both mean "push it"."""
        return self.applied.get(key, _UNKNOWN) if self.readable else _UNKNOWN

    def step(self, label: str, desired: Any, current: Any, apply: Callable[[], bool]) -> bool:
        """Apply one setter unless the device already holds `desired`.
        Returns whether the write was actually issued."""
        if current is not _UNKNOWN and current == desired:
            self.skipped_count += 1
            log.debug("%s: policy push step unchanged, skipping: %s", self.username, label)
            return False
        self.applied_count += 1
        if not apply():
            # Per-call label, not an aggregate: timekpr reduces every
            # failure to a bare result code, so the label is the only
            # record of which call it was.
            log.warning("%s: policy push step failed: %s", self.username, label)
            self.ok = False
        return True


def _apply_policy_push(enforcer: TimekprEnforcer, username: str, policy: dict) -> bool:
    """Apply a hub policy payload to the local timekpr config -- every field
    `PolicyPayload` carries, not just daily/weekly/monthly limits and
    allowed weekdays. Only the fields that differ from what timekpr
    already holds are written -- see `_Push`.

    All-or-nothing on the return value: the caller only advances
    `policy_version_applied` when every needed write succeeded, so a
    partial failure retries whole on the next tick rather than leaving the
    user half-configured (unlike timekpr's own admin GUI, which applies
    fields one call at a time and stops on the first failure)."""
    daily_limits = [int(x) for x in policy["daily_limits_s"]]
    if len(daily_limits) != 7:
        log.error(
            "%s: policy has %d daily limits, timekpr requires 7 -- not applying", username, len(daily_limits)
        )
        return False

    push = _Push(username, enforcer.get_applied_policy(username))
    if not push.readable:
        log.warning("%s: could not read current timekpr config -- pushing every field", username)

    allowed_weekdays = policy.get("allowed_weekdays") or list(_ALL_WEEKDAYS)
    weekdays_written = push.step(
        "setAllowedDays",
        allowed_weekdays,
        push.current("ALLOWED_WEEKDAYS"),
        lambda: enforcer.set_allowed_days(username, allowed_weekdays),
    )
    projected = _project_daily_limits_to_allowed_days(daily_limits, allowed_weekdays)
    push.step(
        "setTimeLimitForDays",
        projected,
        # The readback is positional within the OLD weekday set, so once
        # the days move it is no longer comparable at all.
        _UNKNOWN if weekdays_written else push.current("LIMITS_PER_WEEKDAYS"),
        lambda: enforcer.set_time_limit_for_days(username, projected),
    )
    weekly = int(policy["weekly_limit_s"])
    push.step(
        "setTimeLimitForWeek",
        weekly,
        push.current("LIMIT_PER_WEEK"),
        lambda: enforcer.set_time_limit_for_week(username, weekly),
    )
    monthly = int(policy["monthly_limit_s"])
    push.step(
        "setTimeLimitForMonth",
        monthly,
        push.current("LIMIT_PER_MONTH"),
        lambda: enforcer.set_time_limit_for_month(username, monthly),
    )
    track_inactive = bool(policy.get("track_inactive", False))
    push.step(
        "setTrackInactive",
        track_inactive,
        push.current("TRACK_INACTIVE"),
        lambda: enforcer.set_track_inactive(username, track_inactive),
    )
    hide_tray = bool(policy.get("hide_tray_icon", False))
    push.step(
        "setHideTrayIcon",
        hide_tray,
        push.current("HIDE_TRAY_ICON"),
        lambda: enforcer.set_hide_tray_icon(username, hide_tray),
    )
    _apply_lockout(enforcer, username, policy, push)
    _apply_allowed_hours(enforcer, username, policy.get("allowed_hours") or {}, push)
    _apply_playtime(enforcer, username, policy.get("playtime") or {}, push)

    log.info(
        "%s: policy push -- applied %d, skipped %d (unchanged)",
        username,
        push.applied_count,
        push.skipped_count,
    )
    return push.ok


def _apply_lockout(enforcer: TimekprEnforcer, username: str, policy: dict, push: _Push) -> None:
    """One call carries both the type and the wake window, so both have to
    match to skip it. timekpr reports WAKEUP_HOUR_INTERVAL (as "from;to")
    only for 'suspendwake'; for any other type there is nothing to
    compare."""
    lockout_type = policy.get("lockout_type") or "lock"
    wake_from = policy.get("wake_from") or ""
    wake_to = policy.get("wake_to") or ""

    current_type = push.current("LOCKOUT_TYPE")
    if lockout_type == "suspendwake":
        current_wake = push.current("WAKEUP_HOUR_INTERVAL")
        current = (
            _UNKNOWN
            if current_type is _UNKNOWN or current_wake is _UNKNOWN
            else (current_type, *str(current_wake).split(";", 1))
        )
        desired: Any = (lockout_type, wake_from, wake_to)
    else:
        current = current_type
        desired = lockout_type

    push.step(
        "setLockoutType",
        desired,
        current,
        lambda: enforcer.set_lockout_type(username, lockout_type, wake_from, wake_to),
    )


def _apply_allowed_hours(enforcer: TimekprEnforcer, username: str, allowed_hours: dict, push: _Push) -> None:
    """Push each weekday's `AllowedHourInterval` list. A day *absent* from
    `allowed_hours` is left untouched here rather than pushed as empty --
    see `set_allowed_hours`'s own refusal of an empty dict, and
    `core/timekpr_hub_core/allowed_hours.py::unrestricted()` for how the hub
    itself represents "no restriction" (an explicit all-24-hours entry, not
    a missing key). Each day is compared and logged individually (not just
    an aggregate "allowed_hours failed") -- a single bad day (e.g. one with
    an unaccounted flag or overlapping records timekpr's own config
    rejects) should be diagnosable without guessing which of the 7 it
    was."""
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
        _push_hours_day(enforcer, username, str(day), hours_to_dbus_payload(records), push)


def _push_hours_day(
    enforcer: TimekprEnforcer,
    username: str,
    day: str,
    payload: dict[str, dict[str, int | bool]],
    push: _Push,
) -> None:
    """Separate function only so `day`/`payload` are bound as parameters
    rather than captured from the caller's loop."""
    push.step(
        f"setAllowedHours(day={day})",
        payload,
        push.current(f"ALLOWED_HOURS_{day}"),
        lambda: enforcer.set_allowed_hours(username, day, payload),
    )


def _apply_playtime(enforcer: TimekprEnforcer, username: str, playtime: dict, push: _Push) -> None:
    if not playtime:
        return
    enabled = bool(playtime.get("enabled", False))
    push.step(
        "setPlayTimeEnabled",
        enabled,
        push.current("PLAYTIME_ENABLED"),
        lambda: enforcer.set_playtime_enabled(username, enabled),
    )
    override = bool(playtime.get("override_enabled", False))
    push.step(
        "setPlayTimeLimitOverride",
        override,
        push.current("PLAYTIME_LIMIT_OVERRIDE_ENABLED"),
        lambda: enforcer.set_playtime_limit_override(username, override),
    )
    unaccounted = bool(playtime.get("unaccounted_intervals_enabled", True))
    push.step(
        "setPlayTimeUnaccountedIntervalsEnabled",
        unaccounted,
        push.current("PLAYTIME_UNACCOUNTED_INTERVALS_ENABLED"),
        lambda: enforcer.set_playtime_unaccounted_intervals_enabled(username, unaccounted),
    )
    pt_weekdays = playtime.get("allowed_weekdays") or list(_ALL_WEEKDAYS)
    pt_days_written = push.step(
        "setPlayTimeAllowedDays",
        pt_weekdays,
        push.current("PLAYTIME_ALLOWED_WEEKDAYS"),
        lambda: enforcer.set_playtime_allowed_days(username, pt_weekdays),
    )
    pt_daily_limits = [int(x) for x in (playtime.get("daily_limits_s") or [0] * 7)]
    pt_projected = _project_daily_limits_to_allowed_days(pt_daily_limits, pt_weekdays)
    push.step(
        "setPlayTimeLimitsForDays",
        pt_projected,
        _UNKNOWN if pt_days_written else push.current("PLAYTIME_LIMITS_PER_WEEKDAYS"),
        lambda: enforcer.set_playtime_limits_for_days(username, pt_projected),
    )
    activities = [(a["mask"], a.get("description", "")) for a in (playtime.get("activities") or [])]
    push.step(
        "setPlayTimeActivities",
        activities,
        push.current("PLAYTIME_ACTIVITIES"),
        lambda: enforcer.set_playtime_activities(username, activities),
    )
