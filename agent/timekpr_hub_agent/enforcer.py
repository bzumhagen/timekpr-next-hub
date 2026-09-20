"""Thin wrapper around timekpr's own DBUS admin connector.

Reuses `timekprAdminConnector` wholesale rather than reimplementing the
DBUS layer, and calls `initTimekprConnection(pTryOnce=True)` so it doesn't
schedule GLib retries -- confirmed against the real signature, which takes
no `pIsClient` kwarg; it's
`(pTryOnce, pRescheduleConnection=False, pCLI=None)`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime as _datetime
from typing import Any

from timekpr_hub_agent.timekpr_paths import ensure_timekpr_importable

log = logging.getLogger("timekpr_hub_agent")


@dataclass
class UserObservation:
    """What the agent read from timekpr this tick -- the subset of
    getUserInformation('F')'s payload the convergence controller needs.

    logged_in reflects whether ACTUAL_* keys were present: they are absent
    when the user is logged out, so their absence is the "logged in?" signal
    rather than a KeyError at 2am.
    """

    balance_s: int
    spent_day_s: int
    limit_today_s: int
    logged_in: bool
    active: bool


class TimekprEnforcer:
    """One instance per agent process; talks to the local timekprd over the
    system DBUS. Deliberately synchronous and blocking, so the agent needs
    no GLib main loop and stays a plain, testable `while True` on
    `time.monotonic()` -- DBUS round trips measure ~2ms against a real
    daemon, negligible against any sync interval.
    """

    def __init__(self) -> None:
        # Deferred rather than a module-level import: this module needs to
        # be importable (for `status`, tests, and CI) without timekpr-next
        # actually being installed, and a bare ImportError here would be a
        # confusing traceback instead of TimekprNotFoundError's actionable
        # message (which names exactly where it looked).
        ensure_timekpr_importable()
        from timekpr.client.interface.dbus.administration import timekprAdminConnector

        self._admin = timekprAdminConnector()
        self._connected = False

    def connect(self) -> bool:
        self._admin.initTimekprConnection(pTryOnce=True, pCLI=True)
        # initTimekprConnection swallows its own exceptions and logs; the
        # only externally-visible signal of success is whether the admin
        # interface got populated.
        self._connected = self._admin._timekprUserAdminDbusInterface is not None
        if not self._connected:
            # Previously swallowed entirely -- a user locked out because
            # timekprd wasn't reachable (wrong group, daemon down, DBUS
            # policy misconfigured) looked identical to "everything's fine,
            # just no news yet" in the log.
            log.warning("could not connect to timekprd over DBUS (check group membership / timekprd status)")
        return self._connected

    def _call(self, dbus_method: str, *args: Any) -> bool:
        """Every write-side DBUS call shares this shape: connect on demand,
        call, and reduce timekpr's own (result, message) reply to a bool.
        `dbus_method` is the name of the method on `timekprAdminConnector`."""
        if not self._connected and not self.connect():
            return False
        result, _message = getattr(self._admin, dbus_method)(*args)
        return result == 0

    def get_user_observation(self, username: str) -> UserObservation | None:
        if not self._connected and not self.connect():
            return None

        result, _message, info = self._admin.getUserConfigurationAndInformation(username, "F")
        if result != 0:
            return None

        logged_in = "ACTUAL_TIME_SPENT_DAY" in info
        if logged_in:
            balance = int(info["ACTUAL_TIME_SPENT_BALANCE"])
            spent_day = int(info["ACTUAL_TIME_SPENT_DAY"])
        else:
            balance = int(info["TIME_SPENT_BALANCE"])
            spent_day = int(info["TIME_SPENT_DAY"])

        # `limit_today_s` MUST be the static LIMITS_PER_WEEKDAYS entry for
        # today, NOT `TIME_LEFT_DAY + balance` -- TIME_LEFT_DAY is a
        # dynamically recomputed value that also folds in ALLOWED_HOURS and
        # the week/month min(), so it silently disagrees with what
        # `setTimeLeft(user, '=', secs)` actually uses internally
        # (getUserLimitsPerWeekdays()[isoweekday-1]). Found only by running
        # against a real account with hour restrictions: the wrong value
        # fed a short `seconds` into every '=' write, and no synthetic
        # model reproduced it.
        today_idx = _datetime.now().isoweekday() - 1  # 0=Mon..6=Sun, matches LIMITS_PER_WEEKDAYS order
        limits_per_weekday = [int(x) for x in info["LIMITS_PER_WEEKDAYS"]]
        limit_today = limits_per_weekday[today_idx]

        return UserObservation(
            balance_s=balance,
            spent_day_s=spent_day,
            limit_today_s=limit_today,
            logged_in=logged_in,
            # `active` here is retained only as "is there a session at all" --
            # tick.py's run_tick derives the actual draining/idle distinction
            # from the tick-over-tick burn delta, which is ground truth (it's
            # literally what moved the counter), rather than from timekpr's
            # own idle hint (which would require a second DBUS field and
            # still lags a screen-lock transition by one tick).
            active=logged_in,
        )

    def set_time_left(self, username: str, op: str, seconds: int) -> bool:
        """`op` is one of Op.SET / Op.SUBTRACT / Op.ADD's `.value`
        ('=', '-', '+') from `timekpr_hub_core.convergence`."""
        return self._call("setTimeLeft", username, op, seconds)

    def set_time_limit_for_days(self, username: str, daily_limits_s: list[int]) -> bool:
        return self._call("setTimeLimitForDays", username, daily_limits_s)

    def set_time_limit_for_week(self, username: str, limit_s: int) -> bool:
        return self._call("setTimeLimitForWeek", username, limit_s)

    def set_time_limit_for_month(self, username: str, limit_s: int) -> bool:
        return self._call("setTimeLimitForMonth", username, limit_s)

    def set_allowed_days(self, username: str, weekdays: list[str]) -> bool:
        """`weekdays` are '1'..'7' (Mon..Sun) strings, matching
        `PolicyPayload.allowed_weekdays` and timekpr's own ALLOWED_WEEKDAYS."""
        return self._call("setAllowedDays", username, weekdays)

    def set_allowed_hours(
        self, username: str, day_number: str, hours: dict[str, dict[str, int | bool]]
    ) -> bool:
        """`day_number` is '1'..'7' (matching setAllowedDays); `hours` is
        already in `setAllowedHours`'s per-hour dict shape, string-keyed --
        see `core/timekpr_hub_core/allowed_hours.py::hours_to_dbus_payload`
        for why that key type is load-bearing. Caller MUST also never pass
        an empty `hours` dict: an absent hour means "forbidden" to timekpr,
        so an empty dict would lock the user out of every hour of
        `day_number`, not leave it unrestricted."""
        if not hours:
            log.error("%s: refusing to push an empty allowed_hours for day %s", username, day_number)
            return False
        return self._call("setAllowedHours", username, day_number, hours)

    def set_track_inactive(self, username: str, track_inactive: bool) -> bool:
        return self._call("setTrackInactive", username, track_inactive)

    def set_hide_tray_icon(self, username: str, hide: bool) -> bool:
        return self._call("setHideTrayIcon", username, hide)

    def set_lockout_type(self, username: str, lockout_type: str, wake_from: str, wake_to: str) -> bool:
        """`lockout_type` is one of timekpr's TK_CTRL_RES_* string values
        (see `core.models.LockoutType`); `wake_from`/`wake_to` are only
        meaningful for 'suspendwake' but are always passed through -- timekpr
        itself just stores them regardless."""
        return self._call("setLockoutType", username, lockout_type, wake_from or "", wake_to or "")

    def set_playtime_enabled(self, username: str, enabled: bool) -> bool:
        return self._call("setPlayTimeEnabled", username, enabled)

    def set_playtime_limit_override(self, username: str, override: bool) -> bool:
        return self._call("setPlayTimeLimitOverride", username, override)

    def set_playtime_unaccounted_intervals_enabled(self, username: str, enabled: bool) -> bool:
        return self._call("setPlayTimeUnaccountedIntervalsEnabled", username, enabled)

    def set_playtime_allowed_days(self, username: str, weekdays: list[str]) -> bool:
        return self._call("setPlayTimeAllowedDays", username, weekdays)

    def set_playtime_limits_for_days(self, username: str, daily_limits_s: list[int]) -> bool:
        return self._call("setPlayTimeLimitsForDays", username, daily_limits_s)

    def set_playtime_activities(self, username: str, activities: list[tuple[str, str]]) -> bool:
        """`activities` is a list of (mask, description) pairs -- matches
        `setPlayTimeActivities`'s `saas` signature."""
        return self._call("setPlayTimeActivities", username, [list(a) for a in activities])

    def get_user_policy_snapshot(self, username: str) -> dict | None:
        """This device's own currently-configured limits for `username`, in
        the shape `EnrollRequest`'s per-user policy snapshot expects
        Used only at enroll time, to seed a brand-new hub user's
        policy from whatever this device already has configured, instead of
        always starting from the hub's 1h/day placeholder default."""
        if not self._connected and not self.connect():
            return None
        result, _message, info = self._admin.getUserConfigurationAndInformation(username, "F")
        if result != 0:
            return None
        return {
            "daily_limits_s": [int(x) for x in info["LIMITS_PER_WEEKDAYS"]],
            "weekly_limit_s": int(info["LIMIT_PER_WEEK"]),
            "monthly_limit_s": int(info["LIMIT_PER_MONTH"]),
            "allowed_weekdays": [str(d) for d in info["ALLOWED_WEEKDAYS"]],
        }

    def get_user_list(self) -> list[str]:
        if not self._connected and not self.connect():
            return []
        result, _message, users = self._admin.getUserList()
        if result != 0:
            return []
        return [u[0] for u in users]
