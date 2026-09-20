"""A friendlier, IO-free model of timekpr's per-hour ALLOWED_HOURS encoding.

timekpr stores allowed time-of-day windows *per clock hour*
(`common/utils/misc.py:findHourStartEndMinutes`, `common/utils/config.py:
setUserAllowedHours`): each hour of the day is either absent (forbidden) or
present with an optional `[startMin-endMin]` sub-range and an optional `!`
("unaccounted") flag. An admin thinks in *intervals* ("4pm to 8pm"), not
per-hour records, and the day's real allowed time is the union of those
per-hour records reassembled into intervals -- both timekpr's own daemon
(`server/user/userdata.py:getTimeLimits`) and its GTK admin GUI
(`client/gui/admingui.py:getIntervalList`/`rebuildHoursFromIntervals`)
hand-roll this conversion.

This module is the one place the hub does it, so it can be tested
independently of any HTTP/DBUS/DB code (`core` has no IO dependency) via
Hypothesis property tests against a brute-force per-minute oracle -- see
`tests/unit/test_allowed_hours.py`.

Constraint inherited from the on-disk format: **one clock hour cannot hold
two intervals**. `7:15-9:00` + `9:45-14:30` is representable (hour 9
belongs entirely to the first interval); `7:15-9:15` + `9:45-14:30` is not
(hour 9 would need two disjoint sub-ranges). An interval also breaks
wherever the `unaccounted` flag changes.
"""

from __future__ import annotations

from dataclasses import dataclass

HOURS_IN_DAY = 24


@dataclass(frozen=True, slots=True)
class TimeInterval:
    """A half-open time-of-day interval, in minutes since midnight
    [start_min, end_min), 0..1440. `unaccounted` mirrors timekpr's `!`
    hour flag: time within this interval is not charged against the daily
    allowance, and the user may be active here even at zero balance."""

    start_min: int
    end_min: int
    unaccounted: bool = False

    def __post_init__(self) -> None:
        if not (0 <= self.start_min < self.end_min <= 24 * 60):
            raise ValueError(f"invalid interval [{self.start_min}, {self.end_min})")


@dataclass(frozen=True, slots=True)
class HourRecord:
    """One `ALLOWED_HOURS_<day>` element: hour `hour` is allowed from
    `start_min` to `end_min` *within that hour* (0..60 each), per
    `common/utils/misc.py:findHourStartEndMinutes`."""

    hour: int
    start_min: int
    end_min: int
    unaccounted: bool = False


class IntervalConflictError(ValueError):
    """Raised by `validate_intervals` with a message naming the exact clash,
    rather than timekpr admin GUI's six silently-numbered error classes."""


def unrestricted() -> list[TimeInterval]:
    """The "no restriction" interval set -- all 24 hours, one interval.
    Must be used instead of an empty list whenever a day has no restriction:
    timekpr's own semantics for an hour *absent* from ALLOWED_HOURS is
    "forbidden", not "allowed", so an empty interval list must never be
    pushed to timekpr as though it meant "no restriction" (see
    `PolicyPayload.allowed_hours`'s docstring, and
    `agent/timekpr_hub_agent/policy_push.py::_apply_policy_push`)."""
    return [TimeInterval(0, HOURS_IN_DAY * 60, unaccounted=False)]


def validate_intervals(intervals: list[TimeInterval]) -> None:
    """Raises `IntervalConflictError` naming the first conflict found, or
    returns None if `intervals` can be represented as ALLOWED_HOURS records.

    Two intervals conflict if they overlap, or if they are both non-empty
    within the *same clock hour* (timekpr's one-interval-per-hour
    constraint) even without truly overlapping."""
    ordered = sorted(intervals, key=lambda iv: iv.start_min)
    for a, b in zip(ordered, ordered[1:], strict=False):  # deliberately unequal length (pairwise scan)
        if b.start_min < a.end_min:
            raise IntervalConflictError(
                f"{_fmt(a.start_min)}–{_fmt(a.end_min)} overlaps {_fmt(b.start_min)}–{_fmt(b.end_min)}"
            )
        a_last_hour = (a.end_min - 1) // 60
        b_first_hour = b.start_min // 60
        if a_last_hour == b_first_hour:
            raise IntervalConflictError(
                f"{_fmt(a.start_min)}–{_fmt(a.end_min)} and {_fmt(b.start_min)}–{_fmt(b.end_min)} "
                f"both touch the {a_last_hour:02d}:00 hour — timekpr can only store one "
                "window per clock hour; the second window must start at the top of the next hour"
            )


def _fmt(total_min: int) -> str:
    return f"{total_min // 60:02d}:{total_min % 60:02d}"


def intervals_to_hours(intervals: list[TimeInterval]) -> list[HourRecord]:
    """Expand a validated interval list into timekpr's per-hour records.
    Caller should have already run `validate_intervals` -- this does not
    re-validate, so an invalid (overlapping / same-hour) input produces
    records that silently disagree with each other, exactly as timekpr's own
    encoder does with bad input."""
    records: list[HourRecord] = []
    for interval in intervals:
        first_hour = interval.start_min // 60
        last_hour = (interval.end_min - 1) // 60
        for hour in range(first_hour, last_hour + 1):
            hour_start = hour * 60
            start_min = max(interval.start_min - hour_start, 0)
            end_min = min(interval.end_min - hour_start, 60)
            records.append(HourRecord(hour, start_min, end_min, interval.unaccounted))
    return records


def hours_to_intervals(records: list[HourRecord]) -> list[TimeInterval]:
    """Reassemble timekpr's per-hour records into merged intervals -- the
    inverse of `intervals_to_hours`, for displaying a policy pulled from
    (or seeded from) a device back as intervals rather than raw hours.
    Adjacent hour records merge into one interval only when contiguous
    (end of one == start of the next, mod the hour boundary) AND their
    `unaccounted` flag matches -- a flag change always breaks the interval."""
    ordered = sorted(records, key=lambda r: r.hour)
    intervals: list[TimeInterval] = []
    cur_start: int | None = None
    cur_end: int | None = None
    cur_uacc = False

    for rec in ordered:
        abs_start = rec.hour * 60 + rec.start_min
        abs_end = rec.hour * 60 + rec.end_min
        if abs_start >= abs_end:
            continue
        if cur_end is not None and abs_start == cur_end and rec.unaccounted == cur_uacc:
            cur_end = abs_end
        else:
            if cur_start is not None and cur_end is not None:
                intervals.append(TimeInterval(cur_start, cur_end, cur_uacc))
            cur_start, cur_end, cur_uacc = abs_start, abs_end, rec.unaccounted

    if cur_start is not None and cur_end is not None:
        intervals.append(TimeInterval(cur_start, cur_end, cur_uacc))

    return intervals


def hours_to_dbus_payload(records: list[HourRecord]) -> dict[str, dict[str, int | bool]]:
    """The exact shape `setAllowedHours(user, dayNumber, hourList)` expects:
    `{"<hour>": {"STARTMIN": ..., "ENDMIN": ..., "UACC": ...}}`
    (`server/interface/dbus/daemon.py:654`).

    The hour key MUST be a string, not an int: timekpr's own
    `checkAndSetAllowedHours` iterates stringified keys but indexes back
    into the original dict with that string, so an int-keyed dict raises
    `KeyError` there -- swallowed into a bare `result=-1` with no readable
    error, previously shipped as allowed-hours changes that silently never
    took effect while weekday limits kept reapplying harmlessly forever."""
    return {
        str(r.hour): {"STARTMIN": r.start_min, "ENDMIN": r.end_min, "UACC": r.unaccounted} for r in records
    }
