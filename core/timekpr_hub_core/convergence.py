"""The convergence controller.

PLAN reference: "The core mechanism" and "⚠ The '=' regression trap".

This module is pure and IO-free by design (PLAN "Verification", Layer 1): it
takes a snapshot of what the agent observed and what the hub reported, and
returns a `Plan` describing the single DBUS write (if any) the agent should
make. Nothing here touches DBUS, the network, or the clock — the caller
supplies every value, which is what makes this testable with Hypothesis in
milliseconds and unambiguous to reason about.

Core invariant (verified against the real daemon, see docs/phase0-findings.md):

    setTimeLeft(user, '-', secs) / ('+', secs)
        -> moves TIME_SPENT_BALANCE only, never TIME_SPENT_DAY/WEEK/MONTH.
    setTimeLeft(user, '=', secs)
        -> sets BALANCE := todaysLimit - secs, and MAY discard up to
           TK_SAVE_INTERVAL (30s) of not-yet-flushed TIME_SPENT_DAY, because
           it triggers pPreserveSpent=False on the daemon side.

Definitions (see PLAN for the full derivation):

    L  = effective daily limit today (policy + grants + carryover)
    s  = local TIME_SPENT_DAY (measurement; the agent never writes this)
    B  = local TIME_SPENT_BALANCE (enforcement; the agent's only write)
    G  = hub's global spent-today for this user (wall-clock union or sum)
    R  = G - s                      ("time burned on this user elsewhere")
    O  = B - s                      (the "offset" the agent maintains)

Goal: keep O == R. Because O is invariant under normal (non-agent) activity
(B and s advance by the same delta each tick), the agent only has to correct
for whatever moved O since its last write — including a parent's own
`timekpra` edit, which is detected as an "unexplained" offset change and
reported as a grant rather than fought.

Note on `correction`: expanding R - O = (G - s) - (B - s) = G - B. The
correction a relative write must apply is therefore computed directly
against the *observed balance*, not against the offset -- there is no `s`
term in it at all. Computing it as `G - O` instead (an offset that still
contains `+s`) is a tempting-looking but wrong simplification; it was caught
by `tests/integration/test_multi_device_simulation.py`, whose multi-day
simulation immediately diverged under it. Both a relative op and an absolute
'=' converge BALANCE itself to exactly G; the only difference between them is
whether `s` (the measurement) survives the write intact.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Op(str, Enum):
    """The DBUS ``setTimeLeft`` operation to issue, or NOOP for "nothing to do"."""

    SET = "="
    SUBTRACT = "-"  # consumes time (raises B towards the limit)
    ADD = "+"  # grants time back (lowers B)
    NOOP = "noop"


@dataclass(frozen=True, slots=True)
class ConvergenceConfig:
    deadband_s: int = 5
    """Minimum |correction| before writing anything — avoids a DBUS write (and
    file rewrite) for a couple seconds of rounding jitter every tick."""

    hard_reset_threshold_s: int = 300
    """|correction| beyond this is treated as "something went badly wrong"
    (daemon restart, long offline period, clock jump) — do an authoritative
    '=' reset rather than a relative nudge."""

    grant_epsilon_s: int = 60
    """Minimum |unexplained offset change| before it's reported as a
    parent-initiated local grant rather than dismissed as noise."""

    regression_tolerance_s: int = 90
    """Used by `advance_cumulative`: how large a *decrease* in observed local
    spent time can be attributed to the '=' op's flush-discard artifact
    (bounded by TK_SAVE_INTERVAL=30s; we allow margin) rather than a genuine
    local day rollover."""


@dataclass(frozen=True, slots=True)
class Observation:
    """What the agent read from timekpr this tick."""

    balance_s: int
    """TIME_SPENT_BALANCE (or ACTUAL_TIME_SPENT_BALANCE if logged in)."""

    spent_local_s: int
    """TIME_SPENT_DAY (or ACTUAL_TIME_SPENT_DAY if logged in) — measurement only."""

    limit_today_s: int
    """The DEVICE's own currently-configured daily limit (TIME_LEFT_DAY +
    balance, or equivalently whatever LIMITS_PER_WEEKDAYS resolves to
    locally today). This is NOT necessarily the same value as the hub's
    intended policy limit (`HubTarget.limit_today_s`) -- they only agree
    once the hub's policy has actually been pushed down via
    setTimeLimitForDays. Using the wrong one here is a real bug that was
    only caught by live dogfooding against a real daemon, not by any test:
    the real `setTimeLeft(user, '=', secs)` computes
    `BALANCE := DEVICE'S OWN configured limit - secs`, so any '=' write's
    `secs` argument, and any clamp-avoidance check gating a '='
    (PLAN pitfall #6), must be computed against *this* value, never against
    what the hub believes the limit to be -- see `plan()`'s use below."""


@dataclass(frozen=True, slots=True)
class HubTarget:
    """What the hub told the agent this tick."""

    limit_today_s: int
    global_spent_s: int
    """G: the hub's canonical total spent today for this user, across all devices."""

    suppressed: bool = False
    """One-active-device-at-a-time loser (PLAN §"Bonus features", phase 4) — drive
    this device's balance to the limit regardless of the arithmetic below."""


@dataclass(frozen=True, slots=True)
class Plan:
    op: Op
    seconds: int
    """Argument to setTimeLeft. Meaningless when op is NOOP."""

    new_applied_offset_s: int
    """The offset the agent should remember as "what we last applied", for next
    tick's unexplained-offset (local grant) detection. This is deliberately
    `observed_offset + actually_applied_correction`, never the target `R` —
    using the target would make the un-applied deadband residue look like a
    phantom local grant on every subsequent tick (see PLAN pitfall #2)."""

    local_grant_s: int
    """Positive: a parent appears to have granted time locally via timekpra
    since our last write. Negative: time appears to have been taken away.
    Zero: nothing unexplained. Report to the hub; do not act on it here."""

    reason: str
    """Short machine-readable explanation, for logs/tests."""


def plan(
    observed: Observation,
    target: HubTarget,
    applied_offset_s: int,
    force_absolute: bool,
    cfg: ConvergenceConfig = ConvergenceConfig(),
) -> Plan:
    """Decide the single DBUS write (if any) to make this tick.

    ``applied_offset_s`` is the agent's own memory of the offset (B - s) it
    last established, carried in its persisted state file (PLAN
    "Offline / hub-unreachable").

    ``force_absolute`` should be True exactly once, on the tick where the
    agent detects a canonical-day rollover (PLAN "Canonical rollover") — it
    forces an authoritative '=' write instead of a relative nudge, because a
    relative nudge from a stale offset would carry yesterday's state into the
    new day.
    """
    observed_offset = observed.balance_s - observed.spent_local_s
    unexplained = observed_offset - applied_offset_s
    local_grant_s = -unexplained if abs(unexplained) > cfg.grant_epsilon_s else 0

    if target.suppressed:
        # One-active-device-at-a-time loser: drive balance to the limit so
        # timekpr's own lockout machinery fires, with its normal warnings and
        # configured LOCKOUT_TYPE. We still want a well-defined applied_offset
        # afterwards so re-entering the pool later doesn't look like a grant.
        # setTimeLeft(user, '=', 0) => BALANCE := DEVICE'S limit - 0 == DEVICE'S limit.
        # (Deliberately observed.limit_today_s, not target.limit_today_s --
        # see Observation.limit_today_s's docstring.)
        return Plan(
            op=Op.SET,
            seconds=0,
            new_applied_offset_s=observed.limit_today_s - observed.spent_local_s,
            local_grant_s=local_grant_s,
            reason="suppressed_one_device_at_a_time",
        )

    # correction = R - O, where R = G - s (time spent *elsewhere*) and
    # O = B - s. Expanding: (G - s) - (B - s) = G - B. Deliberately computed
    # directly against observed.balance_s (not via observed_offset) so it
    # carries no spent_local_s term at all: a relative op moves B by exactly
    # `correction`, landing it on B == G, independent of s.
    correction = target.global_spent_s - observed.balance_s

    needs_absolute = (
        force_absolute
        # The clamp this guards against -- min(BALANCE, limit) in '+'/'-'
        # (PLAN pitfall #6) -- is applied by the REAL daemon against the
        # DEVICE's own configured limit, not the hub's target, so that's
        # what this comparison must use too.
        or observed.balance_s > observed.limit_today_s
        or abs(correction) > cfg.hard_reset_threshold_s
    )

    if needs_absolute:
        # setTimeLeft(user, '=', secs) => BALANCE := DEVICE'S limit - secs.
        # We want BALANCE == G, so secs := DEVICE'S limit - G. Using
        # target.limit_today_s here instead (the hub's belief, which may not
        # equal what's actually configured on this device until a policy push
        # has landed) was a real bug, caught only by running the agent
        # against a live daemon: BALANCE ended up at (local_limit - hub_limit)
        # + G instead of G. See docs/agent-live-test-findings.md.
        seconds = observed.limit_today_s - target.global_spent_s
        return Plan(
            op=Op.SET,
            seconds=seconds,
            new_applied_offset_s=target.global_spent_s - observed.spent_local_s,
            local_grant_s=local_grant_s,
            reason=(
                "force_absolute_rollover"
                if force_absolute
                else "balance_exceeds_limit"
                if observed.balance_s > target.limit_today_s
                else "large_divergence"
            ),
        )

    if correction > cfg.deadband_s:
        # Must consume time: B is behind where the global total says it should be.
        return Plan(
            op=Op.SUBTRACT,
            seconds=correction,
            new_applied_offset_s=observed_offset + correction,
            local_grant_s=local_grant_s,
            reason="converge_consume",
        )

    if correction < -cfg.deadband_s:
        # Must give time back: B is ahead of the global total (e.g. another
        # device's contribution shrank, or a hub-side grant was applied).
        return Plan(
            op=Op.ADD,
            seconds=-correction,
            new_applied_offset_s=observed_offset + correction,
            local_grant_s=local_grant_s,
            reason="converge_refund",
        )

    return Plan(
        op=Op.NOOP,
        seconds=0,
        new_applied_offset_s=observed_offset,
        local_grant_s=local_grant_s,
        reason="within_deadband",
    )


@dataclass
class CumulativeState:
    """The agent's own canonical-day cumulative local-spend counter.

    Kept separate from timekpr's own TIME_SPENT_DAY/WEEK/MONTH because those
    reset on timekpr's own (possibly stale) local-clock day boundary, not the
    hub's canonical one (PLAN "Canonical rollover").
    """

    cum_local_s: int = 0
    raw_prev_s: int = 0


def advance_cumulative(
    state: CumulativeState,
    observed_spent_local_s: int,
    cfg: ConvergenceConfig = ConvergenceConfig(),
) -> CumulativeState:
    """Advance the cumulative counter by the genuine local delta this tick.

    Handles the '=' regression trap (PLAN "⚠ The '=' regression trap",
    confirmed empirically in docs/phase0-findings.md §2): a `'='` write can
    make the observed local-spent value jump *backwards* by up to ~30s as an
    artifact of the daemon reloading unflushed state from disk. A naive
    "went backwards => day rolled over => credit everything" rule would then
    double-count the whole day. Disambiguate by magnitude: a genuine local
    rollover drops the value by (limit-scale) thousands of seconds; a flush
    artifact drops it by at most ~30s. The two are never ambiguous in
    practice, so a fixed tolerance well above the save interval is safe.
    """
    prev = state.raw_prev_s
    curr = observed_spent_local_s

    if curr >= prev:
        new_cum = state.cum_local_s + (curr - prev)
    else:
        drop = prev - curr
        if drop <= cfg.regression_tolerance_s:
            new_cum = state.cum_local_s  # '=' flush artifact: credit nothing
        else:
            new_cum = state.cum_local_s + curr  # genuine local rollover: today's fresh count

    return CumulativeState(cum_local_s=new_cum, raw_prev_s=curr)


def reset_for_new_canonical_day(observed_spent_local_s: int) -> CumulativeState:
    """Call when the hub's canonical `day` advances (PLAN "Canonical rollover").

    Baselines at the *current* observed value, whatever it is — correct
    whether or not the device's own local midnight has passed yet.
    """
    return CumulativeState(cum_local_s=0, raw_prev_s=observed_spent_local_s)
