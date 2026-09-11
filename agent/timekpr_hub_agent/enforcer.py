"""Thin wrapper around timekpr's own DBUS admin connector.

PLAN reference: "Agent -- Python 3.11+ (forced, and fine)": reuse
`timekprAdminConnector` wholesale rather than reimplementing the DBUS layer,
and call `initTimekprConnection(pTryOnce=True)` so it doesn't schedule GLib
retries -- confirmed against the real signature in
docs/phase0-findings.md §6 (no `pIsClient` kwarg; it's
`(pTryOnce, pRescheduleConnection=False, pCLI=None)`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime as _datetime

from timekpr_hub_agent.timekpr_paths import ensure_timekpr_importable

log = logging.getLogger("timekpr_hub_agent")


@dataclass
class UserObservation:
    """What the agent read from timekpr this tick -- the subset of
    getUserInformation('F')'s payload the convergence controller needs.

    logged_in reflects whether ACTUAL_* keys were present (PLAN pitfall #7:
    "ACTUAL_* keys are absent when the user is logged out -- don't KeyError
    at 2am; use their absence as the 'logged in?' signal.")
    """

    balance_s: int
    spent_day_s: int
    limit_today_s: int
    logged_in: bool
    active: bool


class TimekprEnforcer:
    """One instance per agent process; talks to the local timekprd over the
    system DBUS. Deliberately synchronous and blocking (PLAN: "the agent
    needs no GLib main loop and is a plain, testable `while True` on
    time.monotonic()") -- DBUS round trips were measured at ~2ms in Phase 0,
    negligible against any planned sync interval.
    """

    def __init__(self) -> None:
        # Deferred rather than a module-level import: main.py needs to be
        # importable (for `status`, tests, and CI) without timekpr-next
        # actually being installed, and a bare ImportError here would be a
        # confusing traceback instead of TimekprNotFoundError's actionable
        # message (which names exactly where it looked).
        ensure_timekpr_importable()
        from timekpr.client.interface.dbus.administration import timekprAdminConnector

        self._admin = timekprAdminConnector()
        self._connected = False

    def connect(self) -> bool:
        self._admin.initTimekprConnection(pTryOnce=True, pCLI=True)
        # initTimekprConnection swallows its own exceptions and logs (see
        # phase0-findings.md §5); the only externally-visible signal of
        # success is whether the admin interface got populated.
        self._connected = self._admin._timekprUserAdminDbusInterface is not None
        if not self._connected:
            # Previously swallowed entirely -- a child locked out because
            # timekprd wasn't reachable (wrong group, daemon down, DBUS
            # policy misconfigured) looked identical to "everything's fine,
            # just no news yet" in the log.
            log.warning("could not connect to timekprd over DBUS (check group membership / timekprd status)")
        return self._connected

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

        # `limit_today_s` MUST be the static configured LIMITS_PER_WEEKDAYS
        # entry for today, NOT `TIME_LEFT_DAY + balance`. TIME_LEFT_DAY is a
        # dynamically recomputed value (recalculateTimeLeft() -- it also
        # folds in ALLOWED_HOURS restrictions and the week/month min()), so
        # deriving a "limit" from it silently disagrees with what
        # `setTimeLeft(user, '=', secs)` actually uses internally
        # (configprocessor.py:718: `getUserLimitsPerWeekdays()[isoweekday-1]`).
        # This was a second, more fundamental bug found only by running the
        # agent against a real account with hour restrictions configured:
        # TIME_LEFT_DAY + balance landed short of the true 86400 static limit
        # by exactly the hour-restricted minutes, which fed the wrong number
        # into every '=' write's `seconds` argument. See
        # docs/agent-live-test-findings.md.
        today_idx = _datetime.now().isoweekday() - 1  # 0=Mon..6=Sun, matches LIMITS_PER_WEEKDAYS order
        limits_per_weekday = [int(x) for x in info["LIMITS_PER_WEEKDAYS"]]
        limit_today = limits_per_weekday[today_idx]

        return UserObservation(
            balance_s=balance,
            spent_day_s=spent_day,
            limit_today_s=limit_today,
            logged_in=logged_in,
            active=logged_in,  # refined once activity/idle detection lands (Phase 2)
        )

    def set_time_left(self, username: str, op: str, seconds: int) -> bool:
        """`op` is one of Op.SET / Op.SUBTRACT / Op.ADD's `.value`
        ('=', '-', '+') from `timekpr_hub_core.convergence`."""
        if not self._connected and not self.connect():
            return False
        result, _message = self._admin.setTimeLeft(username, op, seconds)
        return result == 0

    def set_time_limit_for_days(self, username: str, daily_limits_s: list[int]) -> bool:
        if not self._connected and not self.connect():
            return False
        result, _message = self._admin.setTimeLimitForDays(username, daily_limits_s)
        return result == 0

    def set_time_limit_for_week(self, username: str, limit_s: int) -> bool:
        if not self._connected and not self.connect():
            return False
        result, _message = self._admin.setTimeLimitForWeek(username, limit_s)
        return result == 0

    def set_time_limit_for_month(self, username: str, limit_s: int) -> bool:
        if not self._connected and not self.connect():
            return False
        result, _message = self._admin.setTimeLimitForMonth(username, limit_s)
        return result == 0

    def set_allowed_days(self, username: str, weekdays: list[str]) -> bool:
        """`weekdays` are '1'..'7' (Mon..Sun) strings, matching
        `PolicyPayload.allowed_weekdays` and timekpr's own ALLOWED_WEEKDAYS."""
        if not self._connected and not self.connect():
            return False
        result, _message = self._admin.setAllowedDays(username, weekdays)
        return result == 0

    def get_user_policy_snapshot(self, username: str) -> dict | None:
        """This device's own currently-configured limits for `username`, in
        the shape `EnrollRequest`'s per-user policy snapshot expects
        (Phase 5a). Used only at enroll time, to seed a brand-new hub user's
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
