"""Unit coverage for services/limits.py::combine_limit -- the single formula
that replaced three independent copies (enforcement's `effective_daily_limit`,
the dashboard's batched path, and the usage-history path in
services/summaries.py). Pure and DB-free, so these run in `make test`.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st
from timekpr_hub.services.limits import combine_limit


def test_gated_and_unreleased_is_always_zero_regardless_of_base_or_grants():
    assert combine_limit(base_s=7200, grants_s=1800, gated=True, released=False) == 0


def test_gated_and_released_behaves_like_an_ungated_day():
    assert combine_limit(base_s=7200, grants_s=1800, gated=True, released=True) == 9000


def test_not_gated_ignores_released_flag():
    # `released` is meaningless when the day was never gated -- the combiner
    # must not accidentally key off it.
    assert combine_limit(base_s=3600, grants_s=0, gated=False, released=False) == 3600
    assert combine_limit(base_s=3600, grants_s=0, gated=False, released=True) == 3600


def test_override_replaces_base_grants_still_add_on_top():
    # An override IS base_s here -- the caller (effective_daily_limit /
    # summaries.py) is responsible for substituting it in place of the
    # policy's standing limit; combine_limit itself doesn't know the
    # difference between a policy limit and an override.
    assert combine_limit(base_s=0, grants_s=900, gated=False, released=False) == 900


def test_a_large_negative_grant_floors_at_zero_not_negative():
    assert combine_limit(base_s=1800, grants_s=-99999, gated=False, released=False) == 0


@given(
    base_s=st.integers(min_value=0, max_value=86400),
    grants_s=st.integers(min_value=-86400, max_value=86400),
    gated=st.booleans(),
    released=st.booleans(),
)
@settings(max_examples=300)
def test_result_is_never_negative(base_s, grants_s, gated, released):
    assert combine_limit(base_s=base_s, grants_s=grants_s, gated=gated, released=released) >= 0


@given(
    base_s=st.integers(min_value=0, max_value=86400),
    grants_s=st.integers(min_value=-86400, max_value=86400),
)
@settings(max_examples=200)
def test_gated_unreleased_is_zero_no_matter_what_base_or_grants_are(base_s, grants_s):
    assert combine_limit(base_s=base_s, grants_s=grants_s, gated=True, released=False) == 0
