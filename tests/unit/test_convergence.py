"""Property tests on the pure convergence controller, plus a small model
of timekpr's own balance
arithmetic to check the *effect* of applying a Plan, not just the Plan
itself.
"""

from __future__ import annotations

from dataclasses import dataclass

from hypothesis import given, settings
from hypothesis import strategies as st
from timekpr_hub_core.convergence import (
    ConvergenceConfig,
    CumulativeState,
    HubTarget,
    Observation,
    Op,
    advance_cumulative,
    plan,
    reset_for_new_canonical_day,
)

CFG = ConvergenceConfig()


# ---------------------------------------------------------------------------
# A tiny model of timekpr's own setTimeLeft semantics, verified against a
# real daemon and against server/config/configprocessor.py.
# Used here to check the *effect* of a Plan, not just the Plan's shape.
# ---------------------------------------------------------------------------


@dataclass
class TimekprBalanceModel:
    balance_s: int
    limit_s: int

    def apply(self, op: Op, seconds: int) -> None:
        if op is Op.SET:
            self.balance_s = self.limit_s - seconds
        elif op is Op.SUBTRACT:
            self.balance_s = min(self.balance_s, self.limit_s) + seconds
        elif op is Op.ADD:
            self.balance_s = min(self.balance_s, self.limit_s) - seconds
        elif op is Op.NOOP:
            pass


reasonable_seconds = st.integers(min_value=0, max_value=86400)
offsets = st.integers(min_value=-86400, max_value=86400)


@given(
    balance_s=reasonable_seconds,
    spent_local_s=reasonable_seconds,
    global_spent_s=reasonable_seconds,
    device_limit_s=reasonable_seconds,
    hub_limit_s=reasonable_seconds,
    applied_offset_s=offsets,
)
@settings(max_examples=300)
def test_plan_converges_time_left_to_hub_effective_limit_even_when_device_limit_differs(
    balance_s, spent_local_s, global_spent_s, device_limit_s, hub_limit_s, applied_offset_s
):
    """Generalizes `test_plan_converges_when_balance_within_limit` to the
    case where the device's own configured limit (`device_limit_s`,
    before a policy push has landed, or after a hub-side grant that never
    gets pushed as a local limit change) doesn't match the hub's effective
    limit (`hub_limit_s`, policy + grants). What must converge is TIME LEFT
    -- `device_limit_s - BALANCE` -- to `hub_limit_s - global_spent_s`, not
    BALANCE itself to `global_spent_s` (that's only true when the two
    limits happen to be equal, which the other test below covers)."""
    obs = Observation(balance_s=balance_s, spent_local_s=spent_local_s, limit_today_s=device_limit_s)
    target = HubTarget(limit_today_s=hub_limit_s, global_spent_s=global_spent_s)

    p = plan(obs, target, applied_offset_s, force_absolute=False, cfg=CFG)

    model = TimekprBalanceModel(balance_s=balance_s, limit_s=device_limit_s)
    model.apply(p.op, p.seconds)
    time_left = device_limit_s - model.balance_s
    target_time_left = hub_limit_s - global_spent_s

    if p.op in (Op.SUBTRACT, Op.ADD):
        assert balance_s <= device_limit_s
        assert time_left == target_time_left
    elif p.op is Op.SET:
        assert time_left == target_time_left
    else:  # NOOP
        target_balance = global_spent_s + device_limit_s - hub_limit_s
        assert abs(target_balance - balance_s) <= CFG.deadband_s


@given(
    balance_s=reasonable_seconds,
    spent_local_s=reasonable_seconds,
    global_spent_s=reasonable_seconds,
    limit_today_s=reasonable_seconds,
    applied_offset_s=offsets,
)
@settings(max_examples=300)
def test_plan_converges_when_balance_within_limit(
    balance_s, spent_local_s, global_spent_s, limit_today_s, applied_offset_s
):
    """Whenever the plan issues a write (either a relative '+'/'-' or an
    absolute '='), applying it to the balance model must land BALANCE
    exactly on `global_spent_s`. This is deliberate for both cases: a
    relative op computes `correction = G - B` precisely so that
    `B + correction == G`, and an absolute op sets `B := limit - (limit - G)
    == G` directly. The difference between the two is NOT the resulting
    balance -- it's whether spent_local (the measurement) is left alone
    ('+'/'-') or put at risk of the '=' regression trap.
    """
    obs = Observation(balance_s=balance_s, spent_local_s=spent_local_s, limit_today_s=limit_today_s)
    target = HubTarget(limit_today_s=limit_today_s, global_spent_s=global_spent_s)

    p = plan(obs, target, applied_offset_s, force_absolute=False, cfg=CFG)

    model = TimekprBalanceModel(balance_s=balance_s, limit_s=limit_today_s)
    model.apply(p.op, p.seconds)

    if p.op in (Op.SUBTRACT, Op.ADD):
        # These clamp via min(balance, limit) in the real daemon; our plan()
        # only chooses them when balance_s <= limit_today_s (guarded by the
        # needs_absolute branch), so the clamp should be a no-op here.
        assert balance_s <= limit_today_s
        assert model.balance_s == global_spent_s
    elif p.op is Op.SET:
        assert model.balance_s == global_spent_s
    else:  # NOOP
        assert abs(target.global_spent_s - balance_s) <= CFG.deadband_s


@given(
    balance_s=reasonable_seconds,
    spent_local_s=reasonable_seconds,
    global_spent_s=reasonable_seconds,
    limit_today_s=reasonable_seconds,
    applied_offset_s=offsets,
)
@settings(max_examples=300)
def test_plan_never_grants_more_than_global_remainder(
    balance_s, spent_local_s, global_spent_s, limit_today_s, applied_offset_s
):
    """The resulting balance should never be pushed below the hub's global
    spent value -- i.e. we never hand back more time than the global ledger
    says is actually remaining."""
    obs = Observation(balance_s=balance_s, spent_local_s=spent_local_s, limit_today_s=limit_today_s)
    target = HubTarget(limit_today_s=limit_today_s, global_spent_s=global_spent_s)

    p = plan(obs, target, applied_offset_s, force_absolute=False, cfg=CFG)

    model = TimekprBalanceModel(balance_s=balance_s, limit_s=limit_today_s)
    model.apply(p.op, p.seconds)

    if p.op is not Op.NOOP:
        assert model.balance_s >= global_spent_s - 1  # -1 slack for int rounding


def test_force_absolute_sets_balance_to_global_spent_exactly():
    obs = Observation(balance_s=12345, spent_local_s=6000, limit_today_s=3600)
    target = HubTarget(limit_today_s=3600, global_spent_s=1800)

    p = plan(obs, target, applied_offset_s=0, force_absolute=True, cfg=CFG)

    model = TimekprBalanceModel(balance_s=obs.balance_s, limit_s=obs.limit_today_s)
    model.apply(p.op, p.seconds)

    assert p.op is Op.SET
    assert model.balance_s == target.global_spent_s
    assert p.reason == "force_absolute_rollover"


def test_overspent_balance_uses_absolute_reset_not_clamped_relative_op():
    """If B already exceeds the limit, a '-'/'+' write would clamp via
    min(balance, limit) and silently erase the overspend. plan() must
    choose '=' in this case."""
    obs = Observation(balance_s=5000, spent_local_s=1000, limit_today_s=3600)
    target = HubTarget(limit_today_s=3600, global_spent_s=1000)

    p = plan(obs, target, applied_offset_s=4000, force_absolute=False, cfg=CFG)

    assert p.op is Op.SET
    assert p.reason == "balance_exceeds_limit"


def test_absolute_write_uses_device_limit_not_hub_target_limit():
    """Regression test for a real bug caught only by running the agent
    against a live daemon (not by any synthetic test): before policy push
    exists, a freshly-enrolled device's own configured daily limit (e.g.
    timekpr's unconfigured default of 86400s) can differ wildly from what
    the hub believes the limit to be (e.g. a new policy's 3600s default).
    Using `target.limit_today_s` (the hub's belief) instead of
    `observed.limit_today_s` (the device's actual configured limit) in the
    `seconds` computation was exactly this bug.

    The invariant is stated in terms of time left rather than balance, so
    that it also covers a mismatched limit correctly: TIME LEFT (the only thing that actually matters -- what
    timekpr enforces and what the user sees) must land on
    `target.limit_today_s - target.global_spent_s`, the hub's *effective*
    limit minus its global spent total, regardless of what the device's own
    limit happens to be configured to. See `plan()`'s `target_balance`.
    """
    obs = Observation(balance_s=345, spent_local_s=393, limit_today_s=86400)  # unconfigured device
    target = HubTarget(limit_today_s=3600, global_spent_s=0)  # hub's freshly-created policy

    p = plan(obs, target, applied_offset_s=0, force_absolute=True, cfg=CFG)

    model = TimekprBalanceModel(balance_s=obs.balance_s, limit_s=obs.limit_today_s)
    model.apply(p.op, p.seconds)

    time_left = obs.limit_today_s - model.balance_s
    assert time_left == target.limit_today_s - target.global_spent_s  # == 3600, the hub's intent


# ---------------------------------------------------------------------------
# advance_cumulative: the '=' regression trap
# ---------------------------------------------------------------------------


def test_advance_cumulative_normal_progress():
    state = CumulativeState(cum_local_s=100, raw_prev_s=500)
    state = advance_cumulative(state, 520, CFG)
    assert state.cum_local_s == 120
    assert state.raw_prev_s == 520


def test_advance_cumulative_absorbs_small_regression_from_equals_op():
    """A drop of <= regression_tolerance_s must be silently absorbed (credit
    nothing), per the '=' op's flush-discard artifact confirmed against a
    real daemon."""
    state = CumulativeState(cum_local_s=1000, raw_prev_s=600)
    state = advance_cumulative(state, 580, CFG)  # dropped by 20s -- within 90s tolerance
    assert state.cum_local_s == 1000  # unchanged, not decreased and not double-counted
    assert state.raw_prev_s == 580


def test_advance_cumulative_treats_large_drop_as_genuine_rollover():
    state = CumulativeState(cum_local_s=5000, raw_prev_s=80000)
    state = advance_cumulative(state, 30, CFG)  # dropped by ~80000s -- a real rollover
    assert state.cum_local_s == 5000 + 30
    assert state.raw_prev_s == 30


@given(
    cum0=st.integers(min_value=0, max_value=86400),
    prev=st.integers(min_value=0, max_value=86400),
    delta=st.integers(min_value=0, max_value=90),
)
def test_advance_cumulative_never_double_counts_within_tolerance(cum0, prev, delta):
    """For any drop within tolerance, the cumulative must never increase --
    this is the property that prevents a '=' write from ever causing a
    double-credit of the day's spent time."""
    curr = max(prev - delta, 0)
    state = CumulativeState(cum_local_s=cum0, raw_prev_s=prev)
    new_state = advance_cumulative(state, curr, CFG)
    if curr <= prev:
        assert new_state.cum_local_s <= cum0 + max(curr - prev, 0)


def test_reset_for_new_canonical_day_baselines_at_current_value():
    state = reset_for_new_canonical_day(observed_spent_local_s=4321)
    assert state.cum_local_s == 0
    assert state.raw_prev_s == 4321
