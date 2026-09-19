"""Canonical calendar boundaries.

One household timezone lives on the hub; the hub is the only thing that
computes dates, and it must match timekpr's own boundary rules exactly:

  * day   = local calendar date in the canonical timezone
  * week  = ISO week, Monday start (matches timekpr's own
            ``getUserDateComponentChanges``, which uses ``isocalendar()``)
  * month = calendar month

All functions here are pure and take an explicit ``tz`` — there is no hidden
global timezone state, which is what makes this testable with `time-machine`
without any monkeypatching of `datetime.now`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo


@dataclass(frozen=True, slots=True)
class CanonicalStamp:
    """The calendar identity of one instant, in the household's canonical timezone."""

    day: date
    iso_year: int
    iso_week: int
    month_year: int
    month: int

    @property
    def day_str(self) -> str:
        return self.day.isoformat()

    @property
    def iso_week_str(self) -> str:
        return f"{self.iso_year}-W{self.iso_week:02d}"

    @property
    def month_str(self) -> str:
        return f"{self.month_year:04d}-{self.month:02d}"


def canonical_stamp(instant: datetime, tz: ZoneInfo) -> CanonicalStamp:
    """Compute the canonical day/week/month for an instant.

    ``instant`` must be timezone-aware (either already in ``tz`` or convertible
    to it, e.g. read from a UTC-stored timestamp). Naive datetimes are rejected
    to avoid the exact "which local time did this device mean" ambiguity that
    causes cross-device rollover bugs.
    """
    if instant.tzinfo is None:
        raise ValueError("canonical_stamp requires a timezone-aware datetime")
    local = instant.astimezone(tz)
    iso_year, iso_week, _iso_weekday = local.isocalendar()
    return CanonicalStamp(
        day=local.date(),
        iso_year=iso_year,
        iso_week=iso_week,
        month_year=local.year,
        month=local.month,
    )


def day_changed(prev: date, curr: date) -> bool:
    return prev != curr


def week_changed(prev: date, curr: date) -> bool:
    py, pw, _ = prev.isocalendar()
    cy, cw, _ = curr.isocalendar()
    return (py, pw) != (cy, cw)


def month_changed(prev: date, curr: date) -> bool:
    return (prev.year, prev.month) != (curr.year, curr.month)


def days_in_iso_week(any_day_in_week: date) -> list[date]:
    """Return the 7 calendar dates (Mon..Sun) of the ISO week containing ``any_day_in_week``."""
    monday = any_day_in_week - timedelta(days=any_day_in_week.isoweekday() - 1)
    return [monday + timedelta(days=i) for i in range(7)]
