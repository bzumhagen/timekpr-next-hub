"""Agent entry point: the tick loop.

PLAN reference: "Each agent tick" (full pseudocode) and "Offline / hub-
unreachable" for the degraded-mode handling. Deliberately a plain
`while True` on `time.monotonic()` -- no GLib main loop needed (PLAN
"Repo layout and stack": call `initTimekprConnection(pTryOnce=True)` so
there's nothing to share a loop with).
"""

from __future__ import annotations

import argparse
import logging
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

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
from timekpr_hub_agent.enforcer import TimekprEnforcer
from timekpr_hub_agent.hubclient import DeviceRevokedError, HubClient, HubClientConfig, HubUnreachableError

log = logging.getLogger("timekpr_hub_agent")

CFG = ConvergenceConfig()

# PLAN "Offline / hub-unreachable behavior" defaults.
DEFAULT_OFFLINE_GRACE_S = 900
DEFAULT_OFFLINE_CAP_S = 1800


def _canonical_day_str(dt: datetime) -> str:
    return dt.date().isoformat()


def run_tick(
    *,
    enforcer: TimekprEnforcer,
    hub: HubClient,
    state: state_mod.AgentState,
    managed_users: list[str],
    agent_version: str,
    tz_name: str,
) -> int:
    """One full tick across every managed user. Returns the next poll delay
    in milliseconds (hub-provided when reachable, a local fallback
    otherwise -- PLAN "Overshoot bound and sync interval")."""
    now = datetime.now(UTC)
    sync_users = []
    observations: dict[str, tuple] = {}

    for username in managed_users:
        obs = enforcer.get_user_observation(username)
        if obs is None:
            continue

        user_state = state.user(username)
        today_str = _canonical_day_str(now)
        force_absolute = False
        cum_local_before = user_state.cum_local_s

        if user_state.day != today_str:
            # Canonical day rollover (PLAN "Canonical rollover"): baseline at
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

        observations[username] = (obs, force_absolute)
        sync_users.append(
            {
                "username": username,
                "day": today_str,
                "cumulative_spent_s": user_state.cum_local_s,
                "active_span": {
                    "start": (now - timedelta(seconds=max(burned_this_tick_s, 1))).isoformat(),
                    "end": now.isoformat(),
                    "burned_s": burned_this_tick_s,
                }
                if obs.active and burned_this_tick_s > 0
                else None,
                "observed": {
                    "balance_s": obs.balance_s,
                    "spent_day_s": obs.spent_day_s,
                    "limit_today_s": obs.limit_today_s,
                    "logged_in": obs.logged_in,
                    "active": obs.active,
                },
                "local_grant_s": 0,  # unexplained-offset detection happens per-user below
                "policy_version_applied": user_state.policy_version_applied,
            }
        )

    next_poll_ms = 20000
    try:
        response = hub.sync(
            {
                "agent_time": now.isoformat(),
                "tz": tz_name,
                "ntp_synced": True,  # PLAN: real NTP check is Phase 2
                "agent_version": agent_version,
                "users": sync_users,
            }
        )
        next_poll_ms = response.get("next_poll_ms", next_poll_ms)

        by_username = {u["username"]: u for u in response["users"]}
        for username, (obs, force_absolute) in observations.items():
            user_state = state.user(username)
            resp_user = by_username.get(username)
            if resp_user is None:
                continue

            user_state.last_effective_limit_today_s = resp_user["effective_limit_today_s"]
            user_state.last_global_spent_s = resp_user["global_spent_s"]
            user_state.last_hub_contact_monotonic = time.monotonic()
            if resp_user.get("policy_version"):
                user_state.policy_version_applied = resp_user["policy_version"]

            _apply_convergence(
                enforcer=enforcer,
                username=username,
                obs=obs,
                target=HubTarget(
                    limit_today_s=resp_user["effective_limit_today_s"],
                    global_spent_s=resp_user["global_spent_s"],
                    suppressed=resp_user.get("suppressed", False),
                ),
                user_state=user_state,
                force_absolute=force_absolute,
            )

    except DeviceRevokedError:
        log.error("device token revoked -- entering closed enforcement immediately")
        for username, (obs, _force) in observations.items():
            user_state = state.user(username)
            _apply_offline_policy(
                enforcer=enforcer,
                username=username,
                obs=obs,
                user_state=user_state,
                offline_policy="closed",
                offline_grace_s=0,
                offline_cap_s=0,
            )
    except HubUnreachableError as exc:
        log.warning("hub unreachable: %s -- applying offline policy", exc)
        for username, (obs, _force) in observations.items():
            user_state = state.user(username)
            _apply_offline_policy(
                enforcer=enforcer,
                username=username,
                obs=obs,
                user_state=user_state,
                offline_policy="capped",  # PLAN default; per-user override is hub-side (Phase 2 wiring)
                offline_grace_s=DEFAULT_OFFLINE_GRACE_S,
                offline_cap_s=DEFAULT_OFFLINE_CAP_S,
            )
        next_poll_ms = min(next_poll_ms * 2, 300_000)

    return next_poll_ms


def _apply_convergence(*, enforcer, username, obs, target, user_state, force_absolute) -> None:
    result = plan(
        Observation(balance_s=obs.balance_s, spent_local_s=obs.spent_day_s, limit_today_s=obs.limit_today_s),
        target,
        user_state.applied_offset_s,
        force_absolute=force_absolute,
        cfg=CFG,
    )
    if result.op is not Op.NOOP:
        enforcer.set_time_left(username, result.op.value, result.seconds)
    user_state.applied_offset_s = result.new_applied_offset_s


def _apply_offline_policy(
    *, enforcer, username, obs, user_state, offline_policy, offline_grace_s, offline_cap_s
) -> None:
    """PLAN "Offline / hub-unreachable behavior" table."""
    seconds_since_contact = time.monotonic() - user_state.last_hub_contact_monotonic
    in_grace = seconds_since_contact < offline_grace_s

    if offline_policy == "open" or in_grace:
        return  # keep enforcing against the frozen last-known target; no new write needed

    if offline_policy == "capped":
        capped_limit = min(
            user_state.last_effective_limit_today_s,
            user_state.cum_local_s + offline_cap_s,
        )
        target = HubTarget(limit_today_s=capped_limit, global_spent_s=user_state.last_global_spent_s)
        _apply_convergence(
            enforcer=enforcer,
            username=username,
            obs=obs,
            target=target,
            user_state=user_state,
            force_absolute=False,
        )
    elif offline_policy == "closed":
        target = HubTarget(limit_today_s=obs.limit_today_s, global_spent_s=obs.limit_today_s, suppressed=True)
        _apply_convergence(
            enforcer=enforcer,
            username=username,
            obs=obs,
            target=target,
            user_state=user_state,
            force_absolute=False,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="timekpr-hub-agent")
    parser.add_argument("--hub-url", required=True)
    parser.add_argument("--users", required=True, help="comma-separated list of local usernames to manage")
    parser.add_argument("--tz", default="UTC")
    parser.add_argument("--state-path", default=str(state_mod.DEFAULT_STATE_PATH))
    parser.add_argument("--once", action="store_true", help="run a single tick and exit (for testing)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    enforcer = TimekprEnforcer()
    hub = HubClient(HubClientConfig(base_url=args.hub_url))
    state_path = Path(args.state_path)
    state = state_mod.load(state_path)
    managed_users = [u.strip() for u in args.users.split(",") if u.strip()]

    while True:
        next_poll_ms = run_tick(
            enforcer=enforcer,
            hub=hub,
            state=state,
            managed_users=managed_users,
            agent_version="0.1.0",
            tz_name=args.tz,
        )
        state_mod.save(state, state_path)
        if args.once:
            break
        time.sleep(next_poll_ms / 1000)


if __name__ == "__main__":
    main()
