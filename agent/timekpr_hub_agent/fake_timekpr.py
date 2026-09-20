"""FakeTimekprDaemon — a small model of timekpr's own accounting semantics.

This is NOT a general-purpose timekpr simulator: it reproduces *only* the
specific accounting behaviors that the convergence controller depends on,
each one verified against the real daemon (either by reading
`server/user/userdata.py` / `server/config/configprocessor.py` directly, or
empirically against a running `timekprd`):

  1. Real activity advances BALANCE and SPENT_DAY/WEEK/MONTH *together*, by
     the same delta (userdata.py:449-462).
  2. `setTimeLeft(user, '-'/'+', secs)` moves BALANCE only, via
     `min(BALANCE, limit) ± secs`, and never touches SPENT_DAY/WEEK/MONTH
     (configprocessor.py:718-729; confirmed live against a real daemon).
  3. `setTimeLeft(user, '=', secs)` sets `BALANCE := limit - secs` AND
     triggers `pPreserveSpent=False`, which reloads SPENT_DAY from the
     last-flushed-to-disk value -- discarding whatever real activity had
     accrued in memory since the last save (bounded by TK_SAVE_INTERVAL=30s;
     confirmed live against a real daemon, where 12s of unflushed
     ACTUAL_TIME_SPENT_DAY was lost).
  4. BALANCE is clamped to +/-86400 seconds (TK_LIMIT_PER_DAY-scale bound).
  5. A day rollover zeroes BALANCE and SPENT_DAY (and, when the week/month
     boundary is also crossed, SPENT_WEEK/SPENT_MONTH).

This lets the convergence controller be simulated against *this* model for
thousands of simulated days in milliseconds -- 3 devices x 30 days of
realistic user behavior in under a second -- without needing a real
timekprd, DBUS, or root. It is deliberately re-validated against the real
daemon's *qualitative* behavior in
`tests/integration/test_fake_timekpr_parity.py`, using the exact numbers
observed against a live daemon.
"""

from __future__ import annotations

from dataclasses import dataclass

TK_LIMIT_PER_DAY = 86400
DEFAULT_SAVE_INTERVAL_S = 30


@dataclass
class FakeTimekprDaemon:
    limit_today_s: int = TK_LIMIT_PER_DAY
    save_interval_s: int = DEFAULT_SAVE_INTERVAL_S

    balance_s: int = 0
    spent_day_s: int = 0
    spent_week_s: int = 0
    spent_month_s: int = 0

    # what's actually durable on disk right now -- this is what a '=' write's
    # pPreserveSpent=False reload will read back.
    _saved_spent_day_s: int = 0
    _saved_spent_week_s: int = 0
    _saved_spent_month_s: int = 0

    _elapsed_since_save_s: int = 0

    def tick(self, seconds: int, active: bool = True) -> None:
        """Simulate `seconds` of real elapsed time, `active` seconds of it
        counting towards the user's accounted time (timekpr does not count
        time while TRACK_INACTIVE is False and the session is idle/locked)."""
        if seconds < 0:
            raise ValueError("tick() seconds must be >= 0")
        if active:
            self.balance_s += seconds
            self.spent_day_s += seconds
            self.spent_week_s += seconds
            self.spent_month_s += seconds
        self._elapsed_since_save_s += seconds
        if self._elapsed_since_save_s >= self.save_interval_s:
            self._flush()

    def _flush(self) -> None:
        """Simulates the daemon's own periodic `saveSpent()`
        (userdata.py:653) -- persist the in-memory counters to "disk"."""
        self._saved_spent_day_s = self.spent_day_s
        self._saved_spent_week_s = self.spent_week_s
        self._saved_spent_month_s = self.spent_month_s
        self._elapsed_since_save_s = 0

    def set_time_left(self, op: str, seconds: int) -> None:
        """Simulate the `setTimeLeft` DBUS admin call.

        This always ends with an explicit disk write (checkAndSetTimeLeft ->
        saveControl()), which is why every branch calls `_flush()` -- but for
        '-'/'+' that's a no-op on spent_day (it wasn't touched), while for '='
        it durably commits the *reloaded* (possibly stale) spent_day, which is
        exactly the mechanism that makes the loss permanent rather than
        something the next real tick would silently fix.
        """
        if op == "=":
            self.balance_s = self.limit_today_s - seconds
            # pPreserveSpent=False: adjustTimeSpentFromControl reloads
            # spent_day/week/month from the control file, discarding
            # whatever accrued in memory since the last flush.
            self.spent_day_s = self._saved_spent_day_s
            self.spent_week_s = self._saved_spent_week_s
            self.spent_month_s = self._saved_spent_month_s
        elif op == "-":
            self.balance_s = min(self.balance_s, self.limit_today_s) + seconds
        elif op == "+":
            self.balance_s = min(self.balance_s, self.limit_today_s) - seconds
        else:
            raise ValueError(f"unknown op {op!r}")

        self.balance_s = max(min(self.balance_s, TK_LIMIT_PER_DAY), -TK_LIMIT_PER_DAY)
        self._flush()

    def rollover_day(self, new_limit_today_s: int | None = None) -> None:
        """Simulate a local-midnight day boundary (userdata.py: dayChanged branch)."""
        self.balance_s = 0
        self.spent_day_s = 0
        if new_limit_today_s is not None:
            self.limit_today_s = new_limit_today_s
        self._flush()

    def rollover_week(self) -> None:
        self.spent_week_s = 0
        self._flush()

    def rollover_month(self) -> None:
        self.spent_month_s = 0
        self._flush()

    # --- read-side, mirroring getUserConfigurationAndInformation('F') ---

    def observed_balance(self) -> int:
        return self.balance_s

    def observed_spent_day(self) -> int:
        return self.spent_day_s
