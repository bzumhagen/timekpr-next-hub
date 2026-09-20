"""Validate FakeTimekprDaemon's qualitative behavior against the real
daemon, using the exact scenario and numbers observed against a live
`timekprd`.

This does not require a running timekprd -- it's a regression test that pins
the fake model to what was actually observed, so future edits to
fake_timekpr.py can't silently drift from reality without a red test.
"""

from __future__ import annotations

from tests.fakes.fake_timekpr import FakeTimekprDaemon


def test_relative_ops_never_touch_spent_counters():
    """'-' 60s then '+' 60s must move BALANCE
    and restore it exactly, while SPENT_DAY/WEEK/MONTH never move."""
    d = FakeTimekprDaemon(limit_today_s=86400)
    d.tick(180, active=True)  # matches observed starting SPENT_DAY=180
    before_day, before_week, before_month = d.spent_day_s, d.spent_week_s, d.spent_month_s
    before_balance = d.balance_s

    d.set_time_left("-", 60)
    assert d.balance_s == before_balance + 60
    assert d.spent_day_s == before_day
    assert d.spent_week_s == before_week
    assert d.spent_month_s == before_month

    d.set_time_left("+", 60)
    assert d.balance_s == before_balance
    assert d.spent_day_s == before_day
    assert d.spent_week_s == before_week
    assert d.spent_month_s == before_month


def test_equals_op_regression_matches_observed_shape():
    """An '=' write after real (unflushed)
    activity has accrued causes spent_day to drop back to the last-saved
    value -- the exact mechanism, not just the same order of magnitude.

    Real observation: ACTUAL_TIME_SPENT_DAY dropped 192 -> 180 (a 12s loss)
    because 12s had accrued since the last save when the '=' write landed.
    We reproduce the same shape: some activity accrues, a save interval has
    NOT yet elapsed, and an '=' write discards exactly the unflushed part.
    """
    d = FakeTimekprDaemon(limit_today_s=86400, save_interval_s=30)
    d.tick(180, active=True)  # get to a flushed baseline of 180, matching the real run
    assert d._saved_spent_day_s == 180

    d.tick(12, active=True)  # 12s of unflushed activity, save interval not yet hit
    assert d.spent_day_s == 192  # matches the real ACTUAL_TIME_SPENT_DAY=192 "before"
    assert d._saved_spent_day_s == 180  # not yet flushed -- matches the real TIME_SPENT_DAY=180

    d.set_time_left("=", 120)
    # limit(86400) - 120 == 86280, matching the real observed BALANCE
    assert d.balance_s == 86280
    # the 12s of unflushed activity is discarded -- matches the real 192 -> 180 drop
    assert d.spent_day_s == 180


def test_balance_clamped_to_plus_minus_one_day():
    d = FakeTimekprDaemon(limit_today_s=3600)
    d.set_time_left("-", 999_999)
    assert d.balance_s == 86400
    d.set_time_left("+", 999_999)
    assert d.balance_s == -86400


def test_day_rollover_zeroes_balance_and_spent_day_only():
    d = FakeTimekprDaemon(limit_today_s=3600)
    d.tick(1000, active=True)
    d.spent_week_s = 5000  # simulate accumulated week/month independent of day tick above
    d.spent_month_s = 20000
    d.rollover_day(new_limit_today_s=3600)
    assert d.balance_s == 0
    assert d.spent_day_s == 0
    # week/month untouched by a mere day rollover
    assert d.spent_week_s == 5000
    assert d.spent_month_s == 20000


def test_week_and_month_rollover_independent_of_day():
    d = FakeTimekprDaemon(limit_today_s=3600)
    d.tick(100, active=True)
    d.rollover_week()
    assert d.spent_week_s == 0
    assert d.spent_day_s == 100  # day untouched by a week rollover
    d.rollover_month()
    assert d.spent_month_s == 0
    assert d.spent_day_s == 100
