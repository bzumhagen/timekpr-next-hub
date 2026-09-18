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
    inactive_session_s: int = 0
    """ACTUAL_TIME_INACTIVE_SESSION -- how long the current session has been
    idle, per timekpr's own idle detection (screen lock / logind idle hint).
    0 when logged out."""
    spent_session_s: int = 0
    """ACTUAL_TIME_SPENT_SESSION -- seconds counted in the current session.
    Read alongside inactive_session_s only for completeness/debugging; the
    agent's own activity_state derivation (main.py) uses the tick-over-tick
    burn delta as ground truth instead, since that's exactly what moved the
    counter."""
    track_inactive: bool = False
    """TRACK_INACTIVE -- whether this user's idle time still counts against
    their limit. Informational for now; enforcement already reflects it
    because timekpr itself decides what to count before exposing
    TIME_SPENT_DAY."""


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
            inactive_session_s = int(info.get("ACTUAL_TIME_INACTIVE_SESSION", 0))
            spent_session_s = int(info.get("ACTUAL_TIME_SPENT_SESSION", 0))
        else:
            balance = int(info["TIME_SPENT_BALANCE"])
            spent_day = int(info["TIME_SPENT_DAY"])
            inactive_session_s = 0
            spent_session_s = 0

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
            # `active` here is retained only as "is there a session at all" --
            # main.py's run_tick derives the actual draining/idle distinction
            # from the tick-over-tick burn delta, which is ground truth (it's
            # literally what moved the counter), rather than from timekpr's
            # own idle hint (which would require a second DBUS field and
            # still lags a screen-lock transition by one tick).
            active=logged_in,
            inactive_session_s=inactive_session_s,
            spent_session_s=spent_session_s,
            track_inactive=bool(info.get("TRACK_INACTIVE", False)),
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

    def set_allowed_hours(
        self, username: str, day_number: str, hours: dict[str, dict[str, int | bool]]
    ) -> bool:
        """`day_number` is '1'..'7' (matching setAllowedDays); `hours` is
        already in `setAllowedHours`'s own per-hour dict shape
        (`{"<hour>": {"STARTMIN": ..., "ENDMIN": ..., "UACC": ...}}` --
        see `core/timekpr_hub_core/allowed_hours.py::hours_to_dbus_payload`).
        The hour key MUST be a string ("8", not 8): timekpr's own
        `checkAndSetAllowedHours` re-indexes the dict with a stringified
        key it derives from iterating it, so an int-keyed dict raises a
        KeyError there that its caller swallows into a bare `result=-1` --
        no exception surfaces here, the DBUS call just silently "fails"
        every single tick forever (see hours_to_dbus_payload's docstring
        for the full story -- this was a real, previously-shipped bug).
        Caller MUST also never pass an empty `hours` dict: an absent hour
        means "forbidden" to timekpr, so an empty dict would lock the user
        out of every hour of `day_number`, not leave it unrestricted."""
        if not hours:
            log.error("%s: refusing to push an empty allowed_hours for day %s", username, day_number)
            return False
        if not self._connected and not self.connect():
            return False
        result, _message = self._admin.setAllowedHours(username, day_number, hours)
        return result == 0

    def set_track_inactive(self, username: str, track_inactive: bool) -> bool:
        if not self._connected and not self.connect():
            return False
        result, _message = self._admin.setTrackInactive(username, track_inactive)
        return result == 0

    def set_hide_tray_icon(self, username: str, hide: bool) -> bool:
        if not self._connected and not self.connect():
            return False
        result, _message = self._admin.setHideTrayIcon(username, hide)
        return result == 0

    def set_lockout_type(self, username: str, lockout_type: str, wake_from: str, wake_to: str) -> bool:
        """`lockout_type` is one of timekpr's TK_CTRL_RES_* string values
        (see `core.models.LockoutType`); `wake_from`/`wake_to` are only
        meaningful for 'suspendwake' but are always passed through -- timekpr
        itself just stores them regardless."""
        if not self._connected and not self.connect():
            return False
        result, _message = self._admin.setLockoutType(username, lockout_type, wake_from or "", wake_to or "")
        return result == 0

    def set_playtime_enabled(self, username: str, enabled: bool) -> bool:
        if not self._connected and not self.connect():
            return False
        result, _message = self._admin.setPlayTimeEnabled(username, enabled)
        return result == 0

    def set_playtime_limit_override(self, username: str, override: bool) -> bool:
        if not self._connected and not self.connect():
            return False
        result, _message = self._admin.setPlayTimeLimitOverride(username, override)
        return result == 0

    def set_playtime_unaccounted_intervals_enabled(self, username: str, enabled: bool) -> bool:
        if not self._connected and not self.connect():
            return False
        result, _message = self._admin.setPlayTimeUnaccountedIntervalsEnabled(username, enabled)
        return result == 0

    def set_playtime_allowed_days(self, username: str, weekdays: list[str]) -> bool:
        if not self._connected and not self.connect():
            return False
        result, _message = self._admin.setPlayTimeAllowedDays(username, weekdays)
        return result == 0

    def set_playtime_limits_for_days(self, username: str, daily_limits_s: list[int]) -> bool:
        if not self._connected and not self.connect():
            return False
        result, _message = self._admin.setPlayTimeLimitsForDays(username, daily_limits_s)
        return result == 0

    def set_playtime_activities(self, username: str, activities: list[tuple[str, str]]) -> bool:
        """`activities` is a list of (mask, description) pairs -- matches
        `setPlayTimeActivities`'s `saas` signature."""
        if not self._connected and not self.connect():
            return False
        result, _message = self._admin.setPlayTimeActivities(username, [list(a) for a in activities])
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
