"""Property tests for the interval <-> per-hour ALLOWED_HOURS conversion
(core/timekpr_hub_core/allowed_hours.py): round-trip stability against a
brute-force per-minute oracle, plus the specific correctness issues the
advanced policy editor would otherwise expose silently.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st
from timekpr_hub_core.allowed_hours import (
    HourRecord,
    IntervalConflictError,
    TimeInterval,
    hours_to_dbus_payload,
    hours_to_intervals,
    intervals_to_hours,
    unrestricted,
    validate_intervals,
)

MINUTES_IN_DAY = 24 * 60


def _bruteforce_minutes(intervals: list[TimeInterval]) -> set[int]:
    """Reference oracle: which of the day's 1440 minutes are covered, and by
    which unaccounted-ness -- (minute, unaccounted) pairs so a flag mismatch
    at the same minute is a genuine conflict, not silently ignored."""
    covered: set[tuple[int, bool]] = set()
    for iv in intervals:
        for m in range(iv.start_min, iv.end_min):
            covered.add((m, iv.unaccounted))
    return covered


def _minutes_from_hours(records: list[HourRecord]) -> set[tuple[int, bool]]:
    covered: set[tuple[int, bool]] = set()
    for r in records:
        for m in range(r.hour * 60 + r.start_min, r.hour * 60 + r.end_min):
            covered.add((m, r.unaccounted))
    return covered


@st.composite
def non_conflicting_interval_lists(draw, max_intervals=4):
    """Generate a list of intervals guaranteed not to conflict: draw
    disjoint minute ranges that never share a clock hour, each tagged with
    its own unaccounted flag."""
    n = draw(st.integers(min_value=0, max_value=max_intervals))
    # unique=True is what keeps intervals from sharing a clock hour
    hour_starts = list(range(0, 24))
    chosen_hours = sorted(draw(st.lists(st.sampled_from(hour_starts), min_size=0, max_size=n, unique=True)))
    intervals = []
    for hour in chosen_hours:
        s = draw(st.integers(min_value=0, max_value=58))
        e = draw(st.integers(min_value=s + 1, max_value=60))
        unaccounted = draw(st.booleans())
        intervals.append(TimeInterval(hour * 60 + s, hour * 60 + e, unaccounted))
    return intervals


@given(intervals=non_conflicting_interval_lists())
@settings(max_examples=300)
def test_intervals_to_hours_round_trip_matches_bruteforce(intervals):
    validate_intervals(intervals)  # should never raise for these fixtures
    records = intervals_to_hours(intervals)
    assert _minutes_from_hours(records) == _bruteforce_minutes(intervals)


@given(intervals=non_conflicting_interval_lists())
@settings(max_examples=300)
def test_hours_to_intervals_is_inverse_of_intervals_to_hours(intervals):
    records = intervals_to_hours(intervals)
    rebuilt = hours_to_intervals(records)
    assert _minutes_from_hours(intervals_to_hours(rebuilt)) == _bruteforce_minutes(intervals)


def test_unrestricted_covers_the_whole_day():
    records = intervals_to_hours(unrestricted())
    assert len(records) == 24
    assert all(r.start_min == 0 and r.end_min == 60 and not r.unaccounted for r in records)


def test_validate_rejects_overlap():
    try:
        validate_intervals([TimeInterval(0, 100), TimeInterval(50, 150)])
        raise AssertionError("expected IntervalConflictError")
    except IntervalConflictError:
        pass


def test_validate_rejects_two_intervals_sharing_one_clock_hour():
    # 7:15-9:15 and 9:45-14:30 both touch hour 9 -- timekpr can only store
    # one interval per hour (README's documented constraint).
    try:
        validate_intervals([TimeInterval(7 * 60 + 15, 9 * 60 + 15), TimeInterval(9 * 60 + 45, 14 * 60 + 30)])
        raise AssertionError("expected IntervalConflictError")
    except IntervalConflictError as exc:
        assert "09:00" in str(exc)


def test_validate_allows_adjacent_intervals_at_the_hour_boundary():
    # 7:15-9:00 and 9:45-14:30: hour 9 belongs entirely to the second
    # interval's gap, so this is representable.
    validate_intervals([TimeInterval(7 * 60 + 15, 9 * 60), TimeInterval(9 * 60 + 45, 14 * 60 + 30)])


def test_hours_to_intervals_breaks_on_unaccounted_flag_change():
    records = [
        HourRecord(8, 0, 60, unaccounted=False),
        HourRecord(9, 0, 60, unaccounted=True),
    ]
    intervals = hours_to_intervals(records)
    assert len(intervals) == 2
    assert intervals[0].unaccounted is False
    assert intervals[1].unaccounted is True


def test_hours_to_dbus_payload_keys_the_hour_as_a_string():
    """Regression test for a real, previously-shipped bug: timekpr's own
    `setAllowedHours` DBUS method has in_signature "ssa{sa{si}}" -- the
    outer dict's keys are strings. Its handler
    (server/config/configprocessor.py::checkAndSetAllowedHours) does
    `for rHour in list(map(str, pHourList)): ...; pHourList[rHour][...]` --
    it re-indexes the dict with a *stringified* key it derives by iterating
    it. An int-keyed dict makes every one of those lookups raise KeyError,
    which the surrounding `except Exception:` swallows into a bare
    `result=-1` with no visible exception anywhere in the hub or agent --
    the push just silently "fails" and retries forever. This asserts the
    key type directly (`"8"`, not `8`) so a future regression back to int
    keys is caught here, not live on a real device."""
    records = [HourRecord(hour=8, start_min=0, end_min=60, unaccounted=False)]
    payload = hours_to_dbus_payload(records)
    assert list(payload.keys()) == ["8"]
    assert all(isinstance(key, str) for key in payload)
    assert payload["8"] == {"STARTMIN": 0, "ENDMIN": 60, "UACC": False}
