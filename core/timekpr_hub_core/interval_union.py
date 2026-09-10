"""Wall-clock union of activity intervals.

PLAN reference: "Wall-clock union (the 'burn once' requirement)".

The user chose wall-clock ("burn once") accounting: 30 minutes of activity on
two devices at the same time should consume 30 minutes of budget, not 60.
Per-device cumulative counters can't express that on their own, so each
agent tick also reports the wall-clock span it covered, and the hub computes
the *union* of all devices' spans for the day.

In production this union is computed by Postgres 14+'s ``range_agg`` (see
PLAN "Data model") for performance at scale. This module is the pure-Python
reference implementation: it is used directly by the hub's aggregation layer
when Postgres range types aren't available (e.g. a first cut, or a unit test
that wants to check the SQL query against ground truth without spinning up
Postgres — see PLAN "Verification" Layer 1, "property-test the interval
union against a brute-force per-second set implementation").
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Span:
    """A half-open wall-clock interval [start_s, end_s), in epoch seconds."""

    start_s: int
    end_s: int

    def __post_init__(self) -> None:
        if self.end_s < self.start_s:
            raise ValueError(f"Span end ({self.end_s}) precedes start ({self.start_s})")


def union_seconds(spans: list[Span]) -> int:
    """Total seconds covered by the union of ``spans`` (overlaps not double-counted).

    O(n log n). Empty input -> 0.
    """
    if not spans:
        return 0

    ordered = sorted(spans, key=lambda s: s.start_s)
    total = 0
    cur_start, cur_end = ordered[0].start_s, ordered[0].end_s

    for span in ordered[1:]:
        if span.start_s <= cur_end:
            # overlapping or touching: extend the current merged run
            cur_end = max(cur_end, span.end_s)
        else:
            total += cur_end - cur_start
            cur_start, cur_end = span.start_s, span.end_s

    total += cur_end - cur_start
    return total


def union_seconds_bruteforce(spans: list[Span]) -> int:
    """Reference oracle: a brute-force per-second set implementation.

    Deliberately inefficient (O(total_span_seconds)) — used only in tests to
    validate ``union_seconds`` via Hypothesis property testing, per the plan's
    verification strategy. Never use this in production code.
    """
    covered: set[int] = set()
    for span in spans:
        covered.update(range(span.start_s, span.end_s))
    return len(covered)


def snap_to_grid(timestamp_s: int, grid_s: int = 5) -> int:
    """Snap a timestamp down to a canonical grid.

    PLAN: "both endpoints snapped to a 5-second grid in canonical time so
    near-simultaneous activity on two devices reliably overlaps instead of
    leaving a sliver gap." Two devices reporting spans independently will
    have start/end timestamps that differ by a second or two (tick jitter,
    small clock skew); snapping every endpoint to the *same* grid means spans
    that are "morally" adjacent or overlapping land on identical or
    neighbouring grid points instead of missing each other by a sliver, so
    ``union_seconds`` merges them instead of reporting a false micro-gap.
    Always snapping down (never rounding to nearest) keeps the direction of
    the small resulting error consistent and easy to reason about: each
    endpoint moves at most ``grid_s`` seconds earlier than reality.
    """
    return (timestamp_s // grid_s) * grid_s
