"""Calendar boundary tests, including the ISO-week year-boundary case:
2025-12-29 is ISO week 2026-W01.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from timekpr_hub_core.calendar import (
    canonical_stamp,
    day_changed,
    days_in_iso_week,
    month_bounds,
    month_changed,
    week_changed,
)

TZ = ZoneInfo("America/Denver")


def test_canonical_stamp_basic():
    dt = datetime(2026, 9, 9, 14, 3, 11, tzinfo=TZ)
    stamp = canonical_stamp(dt, TZ)
    assert stamp.day == date(2026, 9, 9)
    assert stamp.iso_week_str == "2026-W37"
    assert stamp.month_str == "2026-09"


def test_canonical_stamp_rejects_naive_datetime():
    with pytest.raises(ValueError):
        canonical_stamp(datetime(2026, 9, 9, 14, 0, 0), TZ)


def test_iso_week_year_boundary():
    """2025-12-29 (a Monday) is the first day of ISO week 2026-W01, not
    2025-W53 or similar -- a classic off-by-one source for week-based
    aggregation."""
    dt = datetime(2025, 12, 29, 12, 0, 0, tzinfo=TZ)
    stamp = canonical_stamp(dt, TZ)
    assert stamp.iso_week_str == "2026-W01"


def test_iso_week_boundary_end_of_year():
    """2025-12-28 (Sunday) belongs to the last ISO week of 2025."""
    dt = datetime(2025, 12, 28, 12, 0, 0, tzinfo=TZ)
    stamp = canonical_stamp(dt, TZ)
    assert stamp.iso_week_str == "2025-W52"


def test_day_changed():
    assert day_changed(date(2026, 9, 9), date(2026, 9, 10)) is True
    assert day_changed(date(2026, 9, 9), date(2026, 9, 9)) is False


def test_week_changed_crosses_sunday_to_monday():
    # 2026-09-13 is a Sunday, 2026-09-14 is the following Monday -> new ISO week.
    assert week_changed(date(2026, 9, 13), date(2026, 9, 14)) is True
    assert week_changed(date(2026, 9, 9), date(2026, 9, 10)) is False


def test_week_changed_at_iso_year_boundary():
    assert week_changed(date(2025, 12, 28), date(2025, 12, 29)) is True


def test_month_changed():
    assert month_changed(date(2026, 9, 30), date(2026, 10, 1)) is True
    assert month_changed(date(2026, 9, 1), date(2026, 9, 30)) is False


def test_days_in_iso_week_starts_monday():
    days = days_in_iso_week(date(2026, 9, 9))  # a Wednesday
    assert days[0].isoweekday() == 1
    assert days[-1].isoweekday() == 7
    assert len(days) == 7
    assert date(2026, 9, 9) in days


def test_month_bounds_mid_month():
    assert month_bounds(date(2026, 9, 9)) == (date(2026, 9, 1), date(2026, 9, 30))


def test_month_bounds_december_rolls_year():
    assert month_bounds(date(2026, 12, 25)) == (date(2026, 12, 1), date(2026, 12, 31))


def test_month_bounds_february_non_leap_year():
    assert month_bounds(date(2026, 2, 3)) == (date(2026, 2, 1), date(2026, 2, 28))


def test_month_bounds_february_leap_year():
    assert month_bounds(date(2028, 2, 3)) == (date(2028, 2, 1), date(2028, 2, 29))
