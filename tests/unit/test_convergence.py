"""Layer 1 verification (PLAN "Verification"): property tests on the pure
convergence controller, plus a small model of timekpr's own balance
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
# A tiny model of timekpr's own setTimeLeft semantics, verified against the
# real daemon in docs/phase0-findings.md and server/config/configprocessor.py.
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
    ('+'/'-') or put at risk of the '=' regression (see PLAN "⚠ The '='
    regression trap").
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


@given(
    balance_s=reasonable_seconds,
    spent_local_s=reasonable_seconds,
    global_spent_s=reasonable_seconds,
    limit_today_s=reasonable_seconds,
)
def test_applied_offset_bookkeeping_zeroes_unexplained_next_tick(
    balance_s, spent_local_s, global_spent_s, limit_today_s
):
    """If nothing external touches the balance between two ticks, applying
    tick N's `new_applied_offset_s` as tick N+1's `applied_offset_s` must
    yield `local_grant_s == 0` on tick N+1 -- i.e. the agent's own writes are
    never mistaken for a parent's local grant (no false positives)."""
    obs = Observation(balance_s=balance_s, spent_local_s=spent_local_s, limit_today_s=limit_today_s)
    target = HubTarget(limit_today_s=limit_today_s, global_spent_s=global_spent_s)

    p1 = plan(obs, target, applied_offset_s=0, force_absolute=False, cfg=CFG)

    model = TimekprBalanceModel(balance_s=balance_s, limit_s=limit_today_s)
    model.apply(p1.op, p1.seconds)

    # Second tick: nothing else changed except the balance our own write produced.
    obs2 = Observation(balance_s=model.balance_s, spent_local_s=spent_local_s, limit_today_s=limit_today_s)
    p2 = plan(obs2, target, applied_offset_s=p1.new_applied_offset_s, force_absolute=False, cfg=CFG)

    assert p2.local_grant_s == 0


@given(
    balance_s=reasonable_seconds,
    spent_local_s=reasonable_seconds,
    limit_today_s=reasonable_seconds,
    parent_grant_s=st.integers(min_value=61, max_value=3600),
)
def test_local_grant_detected_when_parent_edits_balance_out_of_band(
    balance_s, spent_local_s, limit_today_s, parent_grant_s
):
    """A parent running `timekpra --settimeleft +N` moves the balance without
    moving spent_local -- this must show up as a positive local_grant_s."""
    obs = Observation(balance_s=balance_s, spent_local_s=spent_local_s, limit_today_s=limit_today_s)
    target = HubTarget(limit_today_s=limit_today_s, global_spent_s=balance_s - spent_local_s)

    # Establish a baseline applied_offset matching the current (pre-grant) offset.
    baseline_offset = balance_s - spent_local_s

    # Parent grants time: balance moves up by parent_grant_s (more time left),
    # i.e. B decreases in timekpr's "spent" accounting... but the plan module
    # works in offset space, so simulate it as the offset shrinking.
    granted_balance = balance_s - parent_grant_s
    obs_after_grant = Observation(balance_s=granted_balance, spent_local_s=spent_local_s, limit_today_s=limit_today_s)

    p = plan(obs_after_grant, target, applied_offset_s=baseline_offset, force_absolute=False, cfg=CFG)

    assert p.local_grant_s == parent_grant_s


def test_force_absolute_sets_balance_to_global_spent_exactly():
    obs = Observation(balance_s=12345, spent_local_s=6000, limit_today_s=3600)
    target = HubTarget(limit_today_s=3600, global_spent_s=1800)

    p = plan(obs, target, applied_offset_s=0, force_absolute=True, cfg=CFG)

    model = TimekprBalanceModel(balance_s=obs.balance_s, limit_s=obs.limit_today_s)
    model.apply(p.op, p.seconds)

    assert p.op is Op.SET
    assert model.balance_s == target.global_spent_s
    assert p.reason == "force_absolute_rollover"


def test_suppressed_device_is_driven_to_the_limit():
    obs = Observation(balance_s=100, spent_local_s=50, limit_today_s=3600)
    target = HubTarget(limit_today_s=3600, global_spent_s=1800, suppressed=True)

    p = plan(obs, target, applied_offset_s=50, force_absolute=False, cfg=CFG)

    model = TimekprBalanceModel(balance_s=obs.balance_s, limit_s=obs.limit_today_s)
    model.apply(p.op, p.seconds)

    assert model.balance_s == obs.limit_today_s  # zero time left
    assert p.reason == "suppressed_one_device_at_a_time"


def test_overspent_balance_uses_absolute_reset_not_clamped_relative_op():
    """If B already exceeds the limit, a '-'/'+' write would clamp via
    min(balance, limit) and silently erase the overspend (PLAN pitfall #6).
    plan() must choose '=' in this case."""
    obs = Observation(balance_s=5000, spent_local_s=1000, limit_today_s=3600)
    target = HubTarget(limit_today_s=3600, global_spent_s=1000)

    p = plan(obs, target, applied_offset_s=4000, force_absolute=False, cfg=CFG)

    assert p.op is Op.SET
    assert p.reason == "balance_exceeds_limit"


def test_absolute_write_uses_device_limit_not_hub_target_limit():
    """Regression test for a real bug caught only by running the agent
    against a live daemon (not by any synthetic test): before Phase 2's
    policy push exists, a freshly-enrolled device's own configured daily
    limit (e.g. timekpr's unconfigured default of 86400s) can differ wildly
    from what the hub believes the limit to be (e.g. a new policy's 3600s
    default). The '=' write must land BALANCE on `global_spent_s`
    regardless of that mismatch -- using `target.limit_today_s` (the hub's
    belief) instead of `observed.limit_today_s` (the device's actual
    configured limit) in the `seconds` computation was exactly this bug: it
    left BALANCE off by `device_limit - hub_limit` instead of landing on G.
    """
    obs = Observation(balance_s=345, spent_local_s=393, limit_today_s=86400)  # unconfigured device
    target = HubTarget(limit_today_s=3600, global_spent_s=0)  # hub's freshly-created policy

    p = plan(obs, target, applied_offset_s=0, force_absolute=True, cfg=CFG)

    model = TimekprBalanceModel(balance_s=obs.balance_s, limit_s=obs.limit_today_s)
    model.apply(p.op, p.seconds)

    assert model.balance_s == target.global_spent_s  # == 0, not 82800 (the bug's actual result)


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
    nothing), per the '=' op's flush-discard artifact confirmed in
    docs/phase0-findings.md."""
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
