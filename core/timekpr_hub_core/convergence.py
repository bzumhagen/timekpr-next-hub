"""The convergence controller.

Pure and IO-free by design: takes a snapshot of what the agent observed
and what the hub reported, and returns a `Plan` for the single DBUS write
(if any) the agent should make. The caller supplies every value (nothing
here touches DBUS, the network, or the clock), which is what makes this
testable with Hypothesis in milliseconds.

Core invariant, verified against a real `timekprd`:

    setTimeLeft(user, '-', secs) / ('+', secs)
        -> moves TIME_SPENT_BALANCE only, never TIME_SPENT_DAY/WEEK/MONTH.
    setTimeLeft(user, '=', secs)
        -> sets BALANCE := todaysLimit - secs, and MAY discard up to
           TK_SAVE_INTERVAL (30s) of not-yet-flushed TIME_SPENT_DAY, because
           it triggers pPreserveSpent=False on the daemon side.

Definitions:

    s  = local TIME_SPENT_DAY (measurement; the agent never writes this)
    B  = local TIME_SPENT_BALANCE (enforcement; the agent's only write)
    G  = hub's global spent-today for this user (wall-clock union or sum)
    R  = G - s                      ("time burned on this user elsewhere")
    O  = B - s                      (the "offset" the agent maintains)

Goal: keep O == R. O is invariant under normal (non-agent) activity (B and
s advance by the same delta each tick), so the agent only has to correct
for whatever moved O since its last write. The correction is computed as
`G - B` directly (expanding R - O), not `G - O` -- the latter still
contains `s` and is wrong; regressing to it immediately diverges
`tests/integration/test_multi_device_simulation.py`'s multi-day
simulation. See `HubTarget`/`plan()` below for the general case where the
device's own configured limit doesn't yet match the hub's effective one.
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
    balance). Not necessarily equal to the hub's intended limit
    (`HubTarget.limit_today_s`) until a policy push has landed via
    setTimeLimitForDays. The real `setTimeLeft(user, '=', secs)` computes
    `BALANCE := DEVICE'S OWN configured limit - secs`, so any '=' write's
    `secs` and any clamp check gating one must use *this* value, never the
    hub's -- a real, previously-shipped bug no synthetic test caught,
    since the fake and real daemons only diverge on it when the two limits
    differ. See `plan()`'s use below."""


@dataclass(frozen=True, slots=True)
class HubTarget:
    """What the hub told the agent this tick."""

    limit_today_s: int
    """L_eff: the hub's effective daily limit for this user (policy +
    grants). Can differ from `Observation.limit_today_s` (L_dev, the
    device's own configured limit) until a policy push lands, or after a
    hub-side grant (never pushed as a limit change) -- `plan()` converges
    correctly either way."""

    global_spent_s: int
    """G: the hub's canonical total spent today for this user, across all devices."""


@dataclass(frozen=True, slots=True)
class Plan:
    op: Op
    seconds: int
    """Argument to setTimeLeft. Meaningless when op is NOOP."""

    new_applied_offset_s: int
    """The offset the agent should remember as "what we last applied", for next
    tick's convergence bookkeeping. This is deliberately
    `observed_offset + actually_applied_correction`, never the target `R` —
    using the target would make the un-applied deadband residue look like a
    spurious divergence on every subsequent tick."""

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
    last established, carried in its persisted state file.

    ``force_absolute`` should be True exactly once, on the tick where the
    agent detects a canonical-day rollover — it
    forces an authoritative '=' write instead of a relative nudge, because a
    relative nudge from a stale offset would carry yesterday's state into the
    new day.
    """
    observed_offset = observed.balance_s - observed.spent_local_s

    # Target BALANCE this tick is B* = G + (L_dev - L_eff), not plain G:
    # time left is always (device's limit) - BALANCE, so this makes time
    # left come out to L_eff - G regardless of L_dev -- correct whether or
    # not a policy push has landed yet, and correct after a hub-side grant
    # changes L_eff without being pushed down as a limit change. Reduces to
    # B* = G, the original behavior, once L_dev == L_eff.
    target_balance = target.global_spent_s + observed.limit_today_s - target.limit_today_s
    correction = target_balance - observed.balance_s

    needs_absolute = (
        force_absolute
        # The real daemon's min(BALANCE, limit) clamp in '+'/'-' is against
        # the DEVICE's own configured limit, not the hub's target.
        or observed.balance_s > observed.limit_today_s
        or abs(correction) > cfg.hard_reset_threshold_s
    )

    if needs_absolute:
        # setTimeLeft(user, '=', secs) => BALANCE := DEVICE'S limit - secs.
        # secs := L_dev - target_balance = L_eff - G, independent of L_dev
        # (target_balance already absorbed it), so BALANCE still lands on
        # G + L_dev - L_eff exactly and time left is still L_eff - G.
        seconds = target.limit_today_s - target.global_spent_s
        return Plan(
            op=Op.SET,
            seconds=seconds,
            new_applied_offset_s=target_balance - observed.spent_local_s,
            reason=(
                "force_absolute_rollover"
                if force_absolute
                else "balance_exceeds_limit"
                if observed.balance_s > observed.limit_today_s
                else "large_divergence"
            ),
        )

    if correction > cfg.deadband_s:
        # Must consume time: B is behind where the global total says it should be.
        return Plan(
            op=Op.SUBTRACT,
            seconds=correction,
            new_applied_offset_s=observed_offset + correction,
            reason="converge_consume",
        )

    if correction < -cfg.deadband_s:
        # Must give time back: B is ahead of the global total (e.g. another
        # device's contribution shrank, or a hub-side grant was applied).
        return Plan(
            op=Op.ADD,
            seconds=-correction,
            new_applied_offset_s=observed_offset + correction,
            reason="converge_refund",
        )

    return Plan(
        op=Op.NOOP,
        seconds=0,
        new_applied_offset_s=observed_offset,
        reason="within_deadband",
    )


@dataclass
class CumulativeState:
    """The agent's own canonical-day cumulative local-spend counter.

    Kept separate from timekpr's own TIME_SPENT_DAY/WEEK/MONTH because those
    reset on timekpr's own (possibly stale) local-clock day boundary, not the
    hub's canonical one.
    """

    cum_local_s: int = 0
    raw_prev_s: int = 0


def advance_cumulative(
    state: CumulativeState,
    observed_spent_local_s: int,
    cfg: ConvergenceConfig = ConvergenceConfig(),
) -> CumulativeState:
    """Advance the cumulative counter by the genuine local delta this tick.

    Handles the '=' regression trap, confirmed empirically: a `'='` write
    can make the observed local-spent value jump *backwards* by up to ~30s
    (the daemon reloading unflushed state from disk), which a naive "went
    backwards => day rolled over => credit everything" rule would
    double-count. Disambiguated by magnitude: a genuine rollover drops the
    value by thousands of seconds; a flush artifact drops it by at most
    ~30s, well under `cfg.regression_tolerance_s`.
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
    """Call when the hub's canonical `day` advances.

    Baselines at the *current* observed value, whatever it is — correct
    whether or not the device's own local midnight has passed yet.
    """
    return CumulativeState(cum_local_s=0, raw_prev_s=observed_spent_local_s)
