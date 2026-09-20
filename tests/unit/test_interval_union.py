"""Property-test union_seconds against a brute-force per-second oracle."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st
from timekpr_hub_core.interval_union import Span, union_seconds, union_seconds_bruteforce


@st.composite
def span_lists(draw, max_spans=6, max_time=600):
    n = draw(st.integers(min_value=0, max_value=max_spans))
    spans = []
    for _ in range(n):
        start = draw(st.integers(min_value=0, max_value=max_time))
        end = draw(st.integers(min_value=start, max_value=max_time))
        spans.append(Span(start, end))
    return spans


@given(spans=span_lists())
@settings(max_examples=300)
def test_union_seconds_matches_bruteforce_oracle(spans):
    assert union_seconds(spans) == union_seconds_bruteforce(spans)


def test_union_seconds_no_overlap():
    assert union_seconds([Span(0, 10), Span(20, 30)]) == 20


def test_union_seconds_full_overlap():
    assert union_seconds([Span(0, 100), Span(10, 20)]) == 100


def test_union_seconds_touching_spans_merge():
    # Two devices' windows that exactly abut should merge into one continuous span.
    assert union_seconds([Span(0, 10), Span(10, 20)]) == 20


def test_union_seconds_empty():
    assert union_seconds([]) == 0


def test_wallclock_burn_once_two_devices_simultaneous():
    """The core acceptance property behind the user's 'burn once' choice:
    two devices both active for the same 30 minutes must consume only 30
    minutes of global budget, not 60."""
    device_a = Span(0, 1800)
    device_b = Span(0, 1800)
    assert union_seconds([device_a, device_b]) == 1800


def test_wallclock_two_devices_sequential_consumes_sum():
    device_a = Span(0, 1800)
    device_b = Span(1800, 3600)
    assert union_seconds([device_a, device_b]) == 3600
