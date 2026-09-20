"""The tick loop: one full pass over every managed user, each tick.

Deliberately synchronous, called from a plain `while True` in cli.py (no
GLib main loop needed, since `initTimekprConnection(pTryOnce=True)` leaves
nothing to share a loop with).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from timekpr_hub_core.calendar import canonical_stamp
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

from timekpr_hub_agent import state as state_mod
from timekpr_hub_agent.enforcer import TimekprEnforcer, UserObservation
from timekpr_hub_agent.hubclient import DeviceRevokedError, HubClient, HubUnreachableError
from timekpr_hub_agent.policy_push import _apply_policy_push

log = logging.getLogger("timekpr_hub_agent")

CFG = ConvergenceConfig()
DEFAULT_POLL_MS = 20000
# Below timekpr-hub-agent.service's WatchdogSec=120: neither the hub's
# next_poll_ms nor the unreachable-hub backoff (below) may ever sleep past
# this, or systemd kills the unit for missing its watchdog ping.
MAX_POLL_MS = 90000

# Offline / hub-unreachable defaults, matching UserState's own field
# defaults (state.py) -- these are what a user who has never synced
# successfully falls back to. Once the hub has been reached at least once,
# _apply_offline_policy uses the per-user values it actually sent
# (UserState.last_offline_policy/grace_s/cap_s) instead.
DEFAULT_OFFLINE_GRACE_S = 900
DEFAULT_OFFLINE_CAP_S = 1800

# Cap on UserState.pending_spans -- bounds how much state.json can grow
# during a long hub outage. Oldest buffered spans are dropped first; only
# the wall-clock union's accuracy for that window is affected (the absolute
# cumulative_spent_s counter, and hence enforcement, is never lost either
# way -- see docs for global_spent_wallclock's GREATEST floor).
MAX_PENDING_SPANS = 200


def _canonical_day_str(dt: datetime, tz_name: str) -> str:
    """The agent's own best guess at "today", in the household's timezone
    (cached from the hub's last EnrollResponse/SyncResponse -- see
    AgentState.hub_tz), not UTC. Used to decide day rollover *before* the
    hub is contacted this tick, and as the sole source of truth while
    offline. Falls back to UTC if `tz_name` is empty or invalid (e.g. no
    successful sync yet) rather than crashing the tick.

    A wrong guess here only matters for the single tick straddling the
    actual boundary -- the very next sync corrects `AgentState.hub_tz` from
    the hub's authoritative answer, and this function is re-evaluated fresh
    every tick.
    """
    try:
        tz = ZoneInfo(tz_name) if tz_name else ZoneInfo("UTC")
    except (ZoneInfoNotFoundError, ValueError):
        tz = ZoneInfo("UTC")
    return canonical_stamp(dt, tz).day_str


def run_tick(
    *,
    enforcer: TimekprEnforcer,
    hub: HubClient,
    state: state_mod.AgentState,
    managed_users: list[str],
    agent_version: str,
    tz_name: str,
    debug_clock: bool = False,
    now: datetime | None = None,
    previous_poll_ms: int = DEFAULT_POLL_MS,
) -> int:
    """One full tick across every managed user. Returns the next poll delay
    in milliseconds (hub-provided when reachable, exponential backoff off
    `previous_poll_ms` otherwise -- the caller must feed each tick's return
    value back in as the next tick's `previous_poll_ms` for that backoff to
    actually compound). Always clamped to `MAX_POLL_MS`.

    `now`/`debug_clock` exist only for tests/e2e's compressed-time simulation
    (tests/e2e/harness.py) -- `now` is honored ONLY when `debug_clock=True`,
    so a stray `now=` reaching this function some other way is inert rather
    than silently overriding the agent's notion of "today", which drives
    canonical-day rollover, offline grace, and every emitted activity span.
    `_cmd_run` (the systemd/production path) never sets either, and neither
    is exposed as a `run` CLI flag -- nothing an admin or a unit file can
    type should be able to move this clock.
    """
    if debug_clock and now is not None:
        log.debug("run_tick: debug_clock override active, now=%s", now.isoformat())
    else:
        now = datetime.now(UTC)
    sync_users = []
    observations: dict[str, tuple[UserObservation, bool]] = {}
    this_tick_spans: dict[str, dict | None] = {}
    today_str = _canonical_day_str(now, state.hub_tz or tz_name)

    for username in managed_users:
        obs = enforcer.get_user_observation(username)
        if obs is None:
            log.warning("%s: not found in timekpr (check --users / the hub's device enrollment)", username)
            continue

        user_state = state.user(username)
        force_absolute = False
        cum_local_before = user_state.cum_local_s

        if user_state.day == "":
            # First tick ever for this user on this device (fresh enrollment,
            # or a state.json that predates this user being managed): credit
            # whatever timekpr already shows as spent today, rather than
            # silently forgiving it -- an unconditionally-0 baseline would
            # make pre-enrollment usage vanish.
            cum_state = CumulativeState(cum_local_s=obs.spent_day_s, raw_prev_s=obs.spent_day_s)
            user_state.day = today_str
            force_absolute = True
            cum_local_before = 0
        elif user_state.day != today_str:
            # Canonical day rollover: baseline at
            # whatever the device currently shows, whatever its own local
            # clock/rollover state is.
            cum_state = reset_for_new_canonical_day(obs.spent_day_s)
            user_state.day = today_str
            force_absolute = True
            cum_local_before = 0  # a fresh day: nothing to credit as "this tick's delta"
        else:
            cum_state = advance_cumulative(
                CumulativeState(cum_local_s=user_state.cum_local_s, raw_prev_s=user_state.raw_prev_s),
                obs.spent_day_s,
                CFG,
            )

        user_state.cum_local_s = cum_state.cum_local_s
        user_state.raw_prev_s = cum_state.raw_prev_s

        # The genuine local-activity delta credited THIS tick (never
        # negative -- advance_cumulative never decreases cum_local_s).
        burned_this_tick_s = max(cum_state.cum_local_s - cum_local_before, 0)

        # Ground truth for draining vs. idle: the burn delta is exactly what
        # moved the counter this tick, so it needs no separate idle-hint
        # field from timekpr. A first tick (no prior state) has nothing to
        # diff against, so it falls back to `logged_in`.
        if burned_this_tick_s > 0:
            activity_state = "draining"
        elif obs.logged_in:
            activity_state = "idle"
        else:
            activity_state = "logged_out"

        span = None
        if obs.active and burned_this_tick_s > 0:
            # Clamp the start to the end of the previous tick's own emitted
            # span (last_tick_utc) rather than always backdating by
            # burned_this_tick_s from `now`: a delayed tick, a suspend/
            # resume, or the first-tick baseline (which credits the whole of
            # today's pre-existing spent_day_s as one big delta) would
            # otherwise fabricate a start that reaches *before* the previous
            # span's end, and the hub's range_agg union silently swallows
            # that overlap (this was the dominant source of the hub reading
            # low against timekpr's own UI). last_tick_utc == 0.0 means "no
            # prior span this device has ever reported" -- nothing to clamp
            # against yet.
            start = now - timedelta(seconds=burned_this_tick_s)
            if user_state.last_tick_utc:
                start = max(start, datetime.fromtimestamp(user_state.last_tick_utc, UTC))
            span = {"start": start.isoformat(), "end": now.isoformat(), "burned_s": burned_this_tick_s}

        this_tick_spans[username] = span
        observations[username] = (obs, force_absolute)
        sync_users.append(
            {
                "username": username,
                "cumulative_spent_s": user_state.cum_local_s,
                # Buffered spans from a previous failed sync (see the
                # exception handlers below) go out first, oldest first, so
                # the hub's wall-clock union never permanently loses activity
                # to an outage -- insert_activity_interval is idempotent, so
                # replaying an already-recorded one is free.
                "active_spans": user_state.pending_spans + ([span] if span else []),
                "observed": {
                    "balance_s": obs.balance_s,
                    "spent_day_s": obs.spent_day_s,
                    "limit_today_s": obs.limit_today_s,
                    "logged_in": obs.logged_in,
                    "active": obs.active,
                    "activity_state": activity_state,
                },
                "policy_version_applied": user_state.policy_version_applied,
                "policy_revision_applied": user_state.policy_revision_applied,
            }
        )

    next_poll_ms = previous_poll_ms
    try:
        response = hub.sync(
            {
                "agent_time": now.isoformat(),
                "agent_version": agent_version,
                "users": sync_users,
            }
        )
        next_poll_ms = response.get("next_poll_ms", next_poll_ms)

        state.hub_tz = response.get("hub_tz") or state.hub_tz

        by_username = {u["username"]: u for u in response["users"]}
        for username, (obs, force_absolute) in observations.items():
            user_state = state.user(username)
            resp_user = by_username.get(username)
            if resp_user is None:
                continue

            user_state.last_effective_limit_today_s = resp_user["effective_limit_today_s"]
            user_state.last_global_spent_s = resp_user["global_spent_s"]
            user_state.last_hub_contact_utc = now.timestamp()
            user_state.cum_local_at_contact_s = user_state.cum_local_s
            user_state.last_offline_policy = resp_user.get("offline_policy", user_state.last_offline_policy)
            user_state.last_offline_grace_s = resp_user.get(
                "offline_grace_s", user_state.last_offline_grace_s
            )
            user_state.last_offline_cap_s = resp_user.get("offline_cap_s", user_state.last_offline_cap_s)

            # Everything buffered (plus this tick's own span) reached the
            # hub successfully -- drop the buffer, and remember where this
            # tick's span ended so the next tick clamps against it.
            user_state.pending_spans = []
            if this_tick_spans.get(username):
                user_state.last_tick_utc = now.timestamp()

            target = HubTarget(
                limit_today_s=resp_user["effective_limit_today_s"],
                global_spent_s=resp_user["global_spent_s"],
            )

            if resp_user.get("enforcement") == "observe":
                # A device explicitly set to observe-only: compute and log
                # exactly the write that would have been made, but touch
                # neither DBUS (no policy push, no setTimeLeft) nor the
                # agent's own convergence bookkeeping -- see
                # api/admin.py's `set_device_observe_mode` docstring for
                # the contract this has to match.
                if user_state.last_enforcement != "observe":
                    log.warning("%s: hub enforcement is 'observe' -- computing but not writing", username)
                user_state.last_enforcement = "observe"
                _apply_convergence(
                    enforcer=enforcer,
                    username=username,
                    obs=obs,
                    target=target,
                    user_state=user_state,
                    force_absolute=force_absolute,
                    dry_run=True,
                )
                continue
            user_state.last_enforcement = "enforce"

            policy_payload = resp_user.get("policy")
            if policy_payload:
                # Only mark the version applied once every write in the
                # policy actually succeeds -- previously this was set
                # unconditionally off `policy_version` (present on every
                # response, not just a changed one), which told the hub the
                # push had landed on tick 1 even though nothing was ever
                # written to timekpr, and the hub then never sent the
                # payload again.
                if _apply_policy_push(enforcer, username, policy_payload):
                    user_state.policy_version_applied = resp_user["policy_version"]
                    # Both are advanced only together, on success -- the int
                    # for a hub that predates `policy_revision` (it simply
                    # won't be in the response, and `.get` below keeps the
                    # old value rather than clobbering it with ""), the
                    # revision for the one-day hours-override gate (see
                    # `SyncUserRequest.policy_revision_applied`'s docstring).
                    user_state.policy_revision_applied = resp_user.get(
                        "policy_revision", user_state.policy_revision_applied
                    )
                else:
                    log.warning("%s: policy push failed, will retry next tick", username)

            _apply_convergence(
                enforcer=enforcer,
                username=username,
                obs=obs,
                target=target,
                user_state=user_state,
                force_absolute=force_absolute,
            )

    except DeviceRevokedError:
        # Deliberately different from HubUnreachableError below: a 401/403
        # (after hubclient.py's own reload-and-retry has already ruled out
        # "just a stale token from an unfinished re-enroll") means an admin
        # actually removed this device from the hub -- not "can't currently
        # verify usage against the pool", which is what the offline
        # open/capped/closed policies exist for. Forcing a lockout here
        # (the previous behavior: an offline "closed" policy with zero
        # grace) punished a deliberate, authoritative "stop managing this
        # machine" action as if it were an error condition. Instead: touch
        # nothing. Whatever limit/balance timekpr already has stays exactly
        # as it is, so the machine reverts to local self-management -- a
        # admin can reconfigure it directly via timekpra/the timekpr GUI
        # again, same as before this device was ever enrolled. Re-enrolling
        # (which hubclient.py's own retry picks up without even needing a
        # restart) resumes hub management on the very next successful sync.
        for username, (_obs, _force) in observations.items():
            user_state = state.user(username)
            _buffer_unsent_span(user_state, this_tick_spans.get(username))
            if user_state.last_enforcement != "revoked":
                log.error(
                    "%s: device token revoked or removed -- this machine is no longer managed "
                    "by the hub; leaving its local timekpr configuration as-is (self-management "
                    "resumes) until it's re-enrolled",
                    username,
                )
            user_state.last_enforcement = "revoked"
        next_poll_ms = min(next_poll_ms * 2, MAX_POLL_MS)
    except HubUnreachableError as exc:
        log.warning("hub unreachable: %s -- applying offline policy", exc)
        for username, (obs, _force) in observations.items():
            user_state = state.user(username)
            _buffer_unsent_span(user_state, this_tick_spans.get(username))
            _apply_offline_policy(
                enforcer=enforcer,
                username=username,
                obs=obs,
                user_state=user_state,
                # The per-user policy/grace/cap the hub sent on this user's
                # last successful sync (SyncUserResponse.offline_*), cached
                # in state.json for exactly this moment -- an outage is
                # when there's no fresher answer to ask for.
                offline_policy=user_state.last_offline_policy,
                offline_grace_s=user_state.last_offline_grace_s,
                offline_cap_s=user_state.last_offline_cap_s,
                now=now,
            )
        next_poll_ms = min(next_poll_ms * 2, MAX_POLL_MS)

    return min(next_poll_ms, MAX_POLL_MS)


def _buffer_unsent_span(user_state: state_mod.UserState, span: dict | None) -> None:
    """This tick's span never reached the hub (sync raised) -- buffer it so
    the next successful sync replays it instead of the activity vanishing
    from the wall-clock union forever (see UserState.pending_spans). Caps at
    MAX_PENDING_SPANS, dropping the oldest first; cumulative_spent_s (and
    therefore enforcement) is unaffected regardless -- only the union's
    accuracy for whatever gets dropped would be."""
    if span is None:
        return
    user_state.pending_spans.append(span)
    if len(user_state.pending_spans) > MAX_PENDING_SPANS:
        user_state.pending_spans = user_state.pending_spans[-MAX_PENDING_SPANS:]


def _apply_convergence(
    *,
    enforcer: TimekprEnforcer,
    username: str,
    obs: UserObservation,
    target: HubTarget,
    user_state: state_mod.UserState,
    force_absolute: bool,
    dry_run: bool = False,
) -> None:
    """Compute this tick's convergence plan and, unless `dry_run`, apply it.

    In observe mode (`dry_run=True`) the plan is computed and logged
    exactly as it would be applied, but neither `enforcer.set_time_left`
    nor `user_state.applied_offset_s` is touched -- observe mode must have
    zero effect on the device (see api/admin.py's `set_device_observe_mode`
    docstring) and zero effect on the agent's own bookkeeping, so enforcing
    again later starts from the same convergence state as if observe mode
    had never happened."""
    result = plan(
        Observation(balance_s=obs.balance_s, spent_local_s=obs.spent_day_s, limit_today_s=obs.limit_today_s),
        target,
        user_state.applied_offset_s,
        force_absolute=force_absolute,
        cfg=CFG,
    )
    if result.op is not Op.NOOP:
        verb = "would setTimeLeft" if dry_run else "setTimeLeft"
        log.info("%s: %s(%s, %ds) -- %s", username, verb, result.op.value, result.seconds, result.reason)
        if not dry_run:
            enforcer.set_time_left(username, result.op.value, result.seconds)
    if not dry_run:
        user_state.applied_offset_s = result.new_applied_offset_s


def _apply_offline_policy(
    *,
    enforcer: TimekprEnforcer,
    username: str,
    obs: UserObservation,
    user_state: state_mod.UserState,
    offline_policy: str,
    offline_grace_s: int,
    offline_cap_s: int,
    now: datetime,
) -> None:
    """Degraded-mode handling for a device that can't currently be
    *verified* against the pool -- not one an admin has actually removed
    (DeviceRevokedError doesn't route here at all; a revoked/deleted device
    relinquishes control instead). Only `capped` has a caller today
    (HubUnreachableError, `run_tick`); `closed` has none yet, and is kept
    for a future hub-side per-user override rather than being dead code
    left over by accident.

    Uses wall-clock time (epoch seconds), never time.monotonic(): monotonic's
    epoch is arbitrary and resets on reboot, which used to make
    seconds_since_contact deeply negative after a restart -- in_grace read
    True forever and the agent silently stayed unenforced while genuinely
    offline. A negative or implausibly large
    elapsed time here is treated as grace already expired, erring toward
    `capped`/`closed`, never toward silently staying `open`.
    """
    seconds_since_contact = now.timestamp() - user_state.last_hub_contact_utc
    in_grace = 0 <= seconds_since_contact < offline_grace_s

    if offline_policy == "open" or in_grace:
        return  # keep enforcing against the frozen last-known target; no new write needed

    # Estimate today's global spend without ever refunding local activity
    # that happened after the last hub contact -- naively converging to the
    # stale `last_global_spent_s` every tick (the original bug) would
    # refund everything used locally since then, so an offline device would
    # stop counting time at all for as long as it's offline.
    local_since_contact = max(user_state.cum_local_s - user_state.cum_local_at_contact_s, 0)
    estimated_global_spent = user_state.last_global_spent_s + local_since_contact

    if offline_policy == "capped":
        # The ceiling is anchored to the *frozen* last-known spend, not the
        # live estimate -- anchoring it to `estimated_global_spent` instead
        # (a tempting-looking simplification, caught by
        # test_offline_capped_policy_still_enforces_the_cap) would make the
        # cap always trail `offline_cap_s` ahead of current usage and never
        # actually bind, defeating the entire point of an offline cap. The
        # *target* still uses the live, never-refunded estimate, so time
        # left correctly shrinks toward 0 as offline usage approaches it.
        capped_limit = min(
            user_state.last_effective_limit_today_s, user_state.last_global_spent_s + offline_cap_s
        )
        target = HubTarget(limit_today_s=capped_limit, global_spent_s=estimated_global_spent)
        _apply_convergence(
            enforcer=enforcer,
            username=username,
            obs=obs,
            target=target,
            user_state=user_state,
            force_absolute=False,
        )
    elif offline_policy == "closed":
        # Drive the balance to the device's own configured limit -- zero
        # time left -- by making the target agree that the limit is already
        # fully spent. Using obs.limit_today_s for both fields (rather than
        # a dedicated "suppressed" signal) works through the normal
        # convergence path: target_balance collapses to obs.limit_today_s
        # regardless of the device's own limit, same as a real hub-side
        # zero-limit policy would produce.
        target = HubTarget(limit_today_s=obs.limit_today_s, global_spent_s=obs.limit_today_s)
        _apply_convergence(
            enforcer=enforcer,
            username=username,
            obs=obs,
            target=target,
            user_state=user_state,
            force_absolute=False,
        )
