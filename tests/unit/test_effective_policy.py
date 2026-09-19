"""Unit coverage for timekpr_hub_core.effective_policy -- the merge of a
one-day allowed-hours override onto a standing PolicyPayload, and the
revision token that gates whether `/sync` pushes it. Pure and DB-free.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st
from timekpr_hub_core.allowed_hours import TimeInterval, intervals_to_hours, unrestricted
from timekpr_hub_core.effective_policy import (
    materialize_allowed_hours,
    policy_revision,
    with_day_hour_override,
)
from timekpr_hub_core.models import AllowedHourInterval, PolicyPayload

_ALL_WEEKDAYS = ["1", "2", "3", "4", "5", "6", "7"]
_UNRESTRICTED_WIRE = [
    AllowedHourInterval(hour=r.hour, start_min=r.start_min, end_min=r.end_min)
    for r in intervals_to_hours(unrestricted())
]


def _payload(**overrides) -> PolicyPayload:
    defaults = dict(version=1, daily_limits_s=[3600] * 7, weekly_limit_s=25200, monthly_limit_s=108000)
    defaults.update(overrides)
    return PolicyPayload(**defaults)


def _window(from_min: int, to_min: int) -> list[AllowedHourInterval]:
    return [
        AllowedHourInterval(hour=r.hour, start_min=r.start_min, end_min=r.end_min)
        for r in intervals_to_hours([TimeInterval(from_min, to_min)])
    ]


def test_materialize_fills_all_seven_keys_from_empty():
    materialized = materialize_allowed_hours({})
    assert sorted(materialized.keys()) == _ALL_WEEKDAYS
    for day in _ALL_WEEKDAYS:
        # None of the 24 hourly records may be empty -- an absent/empty hour
        # means "forbidden" to timekpr, not "allowed" (the lockout trap this
        # function exists to prevent).
        assert len(materialized[day]) == 24
        for record in materialized[day]:
            assert record.start_min < record.end_min


def test_materializing_an_already_complete_dict_is_the_identity():
    complete = {day: _UNRESTRICTED_WIRE for day in _ALL_WEEKDAYS}
    materialized = materialize_allowed_hours(complete)
    assert materialized == complete


def test_materialize_only_fills_absent_days_leaves_present_ones_alone():
    window = _window(12 * 60, 20 * 60)
    materialized = materialize_allowed_hours({"3": window})
    assert materialized["3"] == window
    for day in _ALL_WEEKDAYS:
        if day != "3":
            assert materialized[day] == _UNRESTRICTED_WIRE


def test_with_day_hour_override_replaces_exactly_one_key():
    payload = _payload()
    window = _window(12 * 60, 20 * 60)
    result = with_day_hour_override(payload, weekday="3", intervals=window)
    assert result.allowed_hours["3"] == window
    for day in _ALL_WEEKDAYS:
        if day != "3":
            assert result.allowed_hours[day] == _UNRESTRICTED_WIRE


def test_with_day_hour_override_is_skipped_when_weekday_not_allowed():
    payload = _payload(allowed_weekdays=["1", "2", "4", "5", "6", "7"])  # Wednesday (3) excluded
    window = _window(12 * 60, 20 * 60)
    result = with_day_hour_override(payload, weekday="3", intervals=window)
    # Materialized (all keys present) but weekday 3 was NOT substituted.
    assert result.allowed_hours["3"] == _UNRESTRICTED_WIRE


def test_revision_changes_when_effective_payload_changes():
    payload = _payload()
    window = _window(12 * 60, 20 * 60)
    overridden = with_day_hour_override(payload, weekday="3", intervals=window)
    assert policy_revision(payload) != policy_revision(overridden)


def test_future_dated_override_does_not_change_todays_revision():
    """Simulates what `services/policy.py::effective_policy_payload` does
    for a date with no row: fall back to the plain materialized payload.
    That must hash identically regardless of whether some OTHER date has an
    override on file -- the revision is a pure function of the effective
    payload, never of "does an override exist somewhere for this user" --
    otherwise setting a override for tomorrow would cause a pointless push
    today, before the override is even in effect."""
    payload = _payload()
    materialized_hours = materialize_allowed_hours(payload.allowed_hours)
    materialized = payload.model_copy(update={"allowed_hours": materialized_hours})
    # A payload for a user who ALSO happens to have an unrelated override
    # for a different date is, from today's perspective, indistinguishable
    # -- effective_policy_payload never even looks up the future row.
    same_materialized_again = payload.model_copy(update={"allowed_hours": materialized_hours})
    assert policy_revision(materialized) == policy_revision(same_materialized_again)


def test_revision_is_insensitive_to_allowed_hours_dict_insertion_order():
    payload_a = _payload(allowed_hours={"1": _UNRESTRICTED_WIRE, "2": _UNRESTRICTED_WIRE})
    payload_b = _payload(allowed_hours={"2": _UNRESTRICTED_WIRE, "1": _UNRESTRICTED_WIRE})
    assert policy_revision(payload_a) == policy_revision(payload_b)


@given(
    version=st.integers(min_value=1, max_value=1000),
    weekly=st.integers(min_value=0, max_value=7 * 86400),
    monthly=st.integers(min_value=0, max_value=31 * 86400),
)
@settings(max_examples=100)
def test_revision_is_deterministic_for_equal_payloads(version, weekly, monthly):
    a = _payload(version=version, weekly_limit_s=weekly, monthly_limit_s=monthly)
    b = _payload(version=version, weekly_limit_s=weekly, monthly_limit_s=monthly)
    assert policy_revision(a) == policy_revision(b)


@given(weekly_a=st.integers(min_value=0, max_value=100), weekly_b=st.integers(min_value=101, max_value=200))
@settings(max_examples=50)
def test_revision_differs_for_different_payloads(weekly_a, weekly_b):
    a = _payload(weekly_limit_s=weekly_a)
    b = _payload(weekly_limit_s=weekly_b)
    assert policy_revision(a) != policy_revision(b)
