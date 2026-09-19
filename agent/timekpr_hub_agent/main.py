"""Agent entry point: the tick loop.

Deliberately a plain `while True` on `time.monotonic()` -- no GLib main
loop needed, since `initTimekprConnection(pTryOnce=True)` leaves nothing to
share a loop with.
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from timekpr_hub_core.allowed_hours import HourRecord, hours_to_dbus_payload
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

from timekpr_hub_agent import config as config_mod
from timekpr_hub_agent import state as state_mod
from timekpr_hub_agent.enforcer import TimekprEnforcer
from timekpr_hub_agent.hubclient import (
    DEFAULT_TOKEN_PATH,
    DeviceRevokedError,
    EnrollError,
    HubClient,
    HubClientConfig,
    HubUnreachableError,
)
from timekpr_hub_agent.notify import sd_notify
from timekpr_hub_agent.timekpr_paths import TimekprNotFoundError

log = logging.getLogger("timekpr_hub_agent")

CFG = ConvergenceConfig()
# Not read via importlib.metadata: the PKGBUILD copies raw .py files into
# system site-packages with no dist-info (see agent/packaging/PKGBUILD's
# header comment), so that lookup would raise PackageNotFoundError on every
# real packaged install. Kept a literal, pinned to agent/pyproject.toml's
# `version` by tests/unit/test_agent_config.py.
AGENT_VERSION = "0.1.0"
MACHINE_ID_PATHS = (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id"))
SERVICE_UNIT = "timekpr-hub-agent.service"

# Offline / hub-unreachable defaults.
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
) -> int:
    """One full tick across every managed user. Returns the next poll delay
    in milliseconds (hub-provided when reachable, a local fallback
    otherwise).

    `now`/`debug_clock` exist only for tests/e2e's compressed-time simulation
    (tests/e2e/harness.py) -- `now` is honored ONLY when `debug_clock=True`,
    so a stray `now=` reaching this function some other way is inert rather
    than silently overriding the agent's notion of "today", which drives
    canonical-day rollover, offline grace, and every emitted activity span.
    `_cmd_run` (the systemd/production path) never sets either, and neither
    is exposed as a `run` CLI flag -- nothing a parent or a unit file can
    type should be able to move this clock.
    """
    if debug_clock and now is not None:
        log.debug("run_tick: debug_clock override active, now=%s", now.isoformat())
    else:
        now = datetime.now(UTC)
    sync_users = []
    observations: dict[str, tuple] = {}
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
                "day": today_str,
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
                "local_grant_s": 0,  # unexplained-offset detection happens per-user below
                "policy_version_applied": user_state.policy_version_applied,
                "policy_revision_applied": user_state.policy_revision_applied,
            }
        )

    next_poll_ms = 20000
    try:
        response = hub.sync(
            {
                "agent_time": now.isoformat(),
                "tz": tz_name,
                "ntp_synced": True,  # no real NTP check yet
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

            # Everything buffered (plus this tick's own span) reached the
            # hub successfully -- drop the buffer, and remember where this
            # tick's span ended so the next tick clamps against it.
            user_state.pending_spans = []
            if this_tick_spans.get(username):
                user_state.last_tick_utc = now.timestamp()

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

            if resp_user.get("enforcement") == "observe":
                # Either an unmapped local username (hub doesn't know this
                # account) or a device explicitly set to observe-only. The
                # hub sends effective_limit_today_s=0/global_spent_s=0 for
                # this case, which would otherwise converge the child
                # straight to locked out -- skip convergence entirely
                # instead, and say so once per transition rather than every
                # 20s tick.
                if user_state.last_enforcement != "observe":
                    log.warning("%s: hub enforcement is 'observe' -- not writing any local limit", username)
                user_state.last_enforcement = "observe"
                continue
            user_state.last_enforcement = "enforce"

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
        # parent can reconfigure it directly via timekpra/the timekpr GUI
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
        next_poll_ms = min(next_poll_ms * 2, 300_000)
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
                offline_policy="capped",  # a per-user override would be hub-side; not wired yet
                offline_grace_s=DEFAULT_OFFLINE_GRACE_S,
                offline_cap_s=DEFAULT_OFFLINE_CAP_S,
                now=now,
            )
        next_poll_ms = min(next_poll_ms * 2, 300_000)

    return next_poll_ms


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


def _apply_convergence(*, enforcer, username, obs, target, user_state, force_absolute) -> None:
    result = plan(
        Observation(balance_s=obs.balance_s, spent_local_s=obs.spent_day_s, limit_today_s=obs.limit_today_s),
        target,
        user_state.applied_offset_s,
        force_absolute=force_absolute,
        cfg=CFG,
    )
    if result.op is not Op.NOOP:
        log.info("%s: setTimeLeft(%s, %ds) -- %s", username, result.op.value, result.seconds, result.reason)
        enforcer.set_time_left(username, result.op.value, result.seconds)
    user_state.applied_offset_s = result.new_applied_offset_s


_ALL_WEEKDAYS = ["1", "2", "3", "4", "5", "6", "7"]


def _project_daily_limits_to_allowed_days(daily_limits: list[int], allowed_weekdays: list[str]) -> list[int]:
    """timekpr indexes LIMITS_PER_WEEKDAYS *positionally within
    ALLOWED_WEEKDAYS*, not by weekday number
    (server/user/userdata.py:265-270, server/config/configprocessor.py:
    107-113 -- both truncate to the shorter of the two lists). The hub
    stores `daily_limits_s` day-keyed (index 0=Mon..6=Sun); pushing it
    verbatim alongside a non-full `allowed_weekdays` would silently hand
    each allowed day the *wrong* day's limit (e.g. disabling Tuesday shifts
    every later day's limit back by one). Project the day-keyed array down
    to exactly the allowed days, in the same order `setAllowedDays` was
    given, so index i of both lists refers to the same weekday."""
    return [daily_limits[int(day) - 1] for day in allowed_weekdays]


def _apply_policy_push(enforcer: TimekprEnforcer, username: str, policy: dict) -> bool:
    """Apply a hub policy payload to the local timekpr config -- every field
    `PolicyPayload` carries, not just daily/weekly/monthly limits and
    allowed weekdays. All-or-nothing: the caller only advances
    `policy_version_applied` when every write below succeeds, so a partial
    failure retries whole on the next tick rather than leaving the user
    half-configured (unlike timekpr's own admin GUI, which applies fields
    one DBUS call at a time and stops on the first failure)."""
    daily_limits = [int(x) for x in policy["daily_limits_s"]]
    if len(daily_limits) != 7:
        log.error(
            "%s: policy has %d daily limits, timekpr requires 7 -- not applying", username, len(daily_limits)
        )
        return False

    ok = True

    def _step(label: str, success: bool) -> bool:
        # Named per-call logging is the whole point: the old bare `ok &=
        # call(...)` chain gave a single aggregate True/False with no way
        # to tell, from the agent's own log, which of the ~10 DBUS calls in
        # a push actually failed -- exactly the gap that made a real,
        # previously-shipped bug (allowed_hours pushed with int keys
        # instead of str, see hours_to_dbus_payload's docstring) invisible
        # from the logs alone: weekday limits kept "succeeding" (retried
        # every tick, harmlessly) while hours silently never applied, and
        # nothing in the log said so.
        nonlocal ok
        if not success:
            log.warning("%s: policy push step failed: %s", username, label)
            ok = False
        return success

    allowed_weekdays = policy.get("allowed_weekdays") or _ALL_WEEKDAYS
    _step("setAllowedDays", enforcer.set_allowed_days(username, allowed_weekdays))
    _step(
        "setTimeLimitForDays",
        enforcer.set_time_limit_for_days(
            username, _project_daily_limits_to_allowed_days(daily_limits, allowed_weekdays)
        ),
    )
    _step("setTimeLimitForWeek", enforcer.set_time_limit_for_week(username, int(policy["weekly_limit_s"])))
    _step("setTimeLimitForMonth", enforcer.set_time_limit_for_month(username, int(policy["monthly_limit_s"])))
    _step(
        "setTrackInactive", enforcer.set_track_inactive(username, bool(policy.get("track_inactive", False)))
    )
    _step("setHideTrayIcon", enforcer.set_hide_tray_icon(username, bool(policy.get("hide_tray_icon", False))))
    _step(
        "setLockoutType",
        enforcer.set_lockout_type(
            username,
            policy.get("lockout_type") or "lock",
            policy.get("wake_from") or "",
            policy.get("wake_to") or "",
        ),
    )
    _apply_allowed_hours(enforcer, username, policy.get("allowed_hours") or {}, _step)
    _apply_playtime(enforcer, username, policy.get("playtime") or {}, _step)
    return ok


def _apply_allowed_hours(
    enforcer: TimekprEnforcer, username: str, allowed_hours: dict, step: Callable[[str, bool], bool]
) -> None:
    """Push each weekday's `AllowedHourInterval` list. A day *absent* from
    `allowed_hours` is left untouched here rather than pushed as empty --
    see `set_allowed_hours`'s own refusal of an empty dict, and
    `core/timekpr_hub_core/allowed_hours.py::unrestricted()` for how the hub
    itself represents "no restriction" (an explicit all-24-hours entry, not
    a missing key). Each day is logged individually via `step` (not just an
    aggregate "allowed_hours failed") -- a single bad day (e.g. one with an
    unaccounted flag or overlapping records timekpr's own config rejects)
    should be diagnosable without guessing which of the 7 it was."""
    for day, intervals in allowed_hours.items():
        records = [
            HourRecord(
                hour=int(iv["hour"]),
                start_min=int(iv["start_min"]),
                end_min=int(iv["end_min"]),
                unaccounted=bool(iv.get("unaccounted", False)),
            )
            for iv in intervals
        ]
        payload = hours_to_dbus_payload(records)
        step(f"setAllowedHours(day={day})", enforcer.set_allowed_hours(username, str(day), payload))


def _apply_playtime(
    enforcer: TimekprEnforcer, username: str, playtime: dict, step: Callable[[str, bool], bool]
) -> None:
    if not playtime:
        return
    step("setPlayTimeEnabled", enforcer.set_playtime_enabled(username, bool(playtime.get("enabled", False))))
    step(
        "setPlayTimeLimitOverride",
        enforcer.set_playtime_limit_override(username, bool(playtime.get("override_enabled", False))),
    )
    step(
        "setPlayTimeUnaccountedIntervalsEnabled",
        enforcer.set_playtime_unaccounted_intervals_enabled(
            username, bool(playtime.get("unaccounted_intervals_enabled", True))
        ),
    )
    pt_weekdays = playtime.get("allowed_weekdays") or _ALL_WEEKDAYS
    step("setPlayTimeAllowedDays", enforcer.set_playtime_allowed_days(username, pt_weekdays))
    pt_daily_limits = [int(x) for x in (playtime.get("daily_limits_s") or [0] * 7)]
    step(
        "setPlayTimeLimitsForDays",
        enforcer.set_playtime_limits_for_days(
            username, _project_daily_limits_to_allowed_days(pt_daily_limits, pt_weekdays)
        ),
    )
    activities = [(a["mask"], a.get("description", "")) for a in (playtime.get("activities") or [])]
    step("setPlayTimeActivities", enforcer.set_playtime_activities(username, activities))


def _apply_offline_policy(
    *, enforcer, username, obs, user_state, offline_policy, offline_grace_s, offline_cap_s, now
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
        target = HubTarget(limit_today_s=obs.limit_today_s, global_spent_s=obs.limit_today_s, suppressed=True)
        _apply_convergence(
            enforcer=enforcer,
            username=username,
            obs=obs,
            target=target,
            user_state=user_state,
            force_absolute=False,
        )


def _read_machine_id() -> str:
    for path in MACHINE_ID_PATHS:
        try:
            return path.read_text().strip()
        except OSError:
            continue
    raise RuntimeError(f"could not read a machine id from any of {[str(p) for p in MACHINE_ID_PATHS]}")


def _add_hub_connection_args(
    parser: argparse.ArgumentParser, env_values: dict[str, str], *, prompt_if_missing: bool = False
) -> None:
    parser.add_argument(
        "--hub-url",
        default=config_mod.env_default("TIMEKPR_HUB_URL", env_values),
        # `run` (prompt_if_missing=False) keeps today's behavior: argparse
        # itself rejects a missing value up front, since `run` is what
        # systemd launches non-interactively and a clear "the following
        # arguments are required" beats a confusing failure three calls
        # later. `enroll` (prompt_if_missing=True) never argparse-requires
        # it -- a parent running it bare gets prompted instead (see
        # _prompt_or_die in _cmd_enroll), and a non-interactive caller still
        # gets a clean, equivalent error from that same helper.
        required=(not prompt_if_missing) and config_mod.env_default("TIMEKPR_HUB_URL", env_values) is None,
        help="e.g. http://hub.local:8000 (http:// is assumed if you omit a scheme)"
        + ("; prompted if omitted" if prompt_if_missing else ""),
    )
    parser.add_argument(
        "--token-path", default=str(DEFAULT_TOKEN_PATH), help="where the device bearer token lives"
    )
    parser.add_argument(
        "--ca-cert",
        default=config_mod.env_default("TIMEKPR_HUB_CA_CERT", env_values),
        help="path to a CA bundle, for a hub with a self-signed cert",
    )


def _prompt_or_die(value: str | None, *, label: str, flag: str) -> str:
    """The interactive-input pattern shared by every prompted `enroll`
    argument (users, hub URL, code): fall through to a clean, actionable
    error rather than blocking forever when stdin isn't a terminal (a
    systemd unit, a script, CI) -- bare `input()` there would hang or raise
    EOFError instead of naming the flag to pass explicitly."""
    if value:
        return value
    if not sys.stdin.isatty():
        raise SystemExit(f"error: {flag} is required when not running interactively")
    entered = input(f"{label}: ").strip()
    if not entered:
        raise SystemExit(f"error: no {label.lower()} given")
    return entered


def _validate_users(enforcer: TimekprEnforcer, requested: list[str]) -> list[str]:
    """Reject usernames timekpr doesn't know about, instead of silently
    skipping them tick after tick. Best-effort: if the user list can't be
    read at all (e.g.
    timekprd not reachable right now), fall back to trusting the caller
    rather than blocking enrollment on a transient DBUS hiccup."""
    known = enforcer.get_user_list()
    if not known:
        return requested
    unknown = [u for u in requested if u not in known]
    if unknown:
        raise SystemExit(
            f"error: {', '.join(unknown)} not found in timekpr. "
            f"Users timekpr knows about: {', '.join(known) or '(none configured)'}"
        )
    return requested


def _prompt_for_users(enforcer: TimekprEnforcer) -> str:
    known = enforcer.get_user_list()
    if not known:
        raise SystemExit(
            "error: --users not given and could not list timekpr's users -- pass --users explicitly"
        )
    print(f"Users timekpr knows about: {', '.join(known)}")
    chosen = input("Which should this hub manage? (comma-separated): ").strip()
    if not chosen:
        raise SystemExit("error: no users selected")
    return chosen


def _cmd_enroll(args: argparse.Namespace) -> None:
    try:
        enforcer = TimekprEnforcer()
        timekprd_ok = enforcer.connect()
    except TimekprNotFoundError as exc:
        raise SystemExit(f"error: {exc}") from None
    print(
        "✓ timekpr-next found"
        + (" and timekprd reachable" if timekprd_ok else " (timekprd not reachable yet -- continuing anyway)")
    )

    users_arg = args.users
    if not users_arg:
        if not sys.stdin.isatty():
            raise SystemExit("error: --users is required when not running interactively")
        users_arg = _prompt_for_users(enforcer)
    local_users = [u.strip() for u in users_arg.split(",") if u.strip()]
    if timekprd_ok:
        local_users = _validate_users(enforcer, local_users)

    local_policies = {}
    for username in local_users:
        snapshot = enforcer.get_user_policy_snapshot(username) if timekprd_ok else None
        if snapshot:
            local_policies[username] = snapshot

    hub_url_input = _prompt_or_die(
        args.hub_url, label="Hub URL (e.g. http://hub.local:8000)", flag="--hub-url"
    )
    try:
        hub_url = config_mod.normalize_hub_url(hub_url_input)
    except config_mod.InvalidHubUrlError as exc:
        raise SystemExit(f"error: {exc}") from None
    code = _prompt_or_die(args.code, label="Enrollment code", flag="--code")

    hub = HubClient(HubClientConfig(base_url=hub_url, token_path=Path(args.token_path), ca_cert=args.ca_cert))
    hostname = args.hostname or socket.gethostname()
    machine_id = args.machine_id or _read_machine_id()

    try:
        data = hub.enroll(
            enrollment_code=code,
            hostname=hostname,
            machine_id=machine_id,
            os=args.os,
            tz=args.tz,
            agent_version=AGENT_VERSION,
            local_users=local_users,
            local_policies=local_policies,
        )
    except EnrollError as exc:
        raise SystemExit(f"error: {exc}") from None

    config_mod.chown_to_service_user(Path(args.token_path))
    if data.get("rebound"):
        # Same machine_id as an existing, non-revoked device -- the hub
        # rotated that device's token and reused its row instead of forking
        # a second history for the same machine (e.g. after `pacman -R` +
        # `pacman -U` and a re-enroll). Say so explicitly: silently doing
        # this without telling the parent looks identical to a fresh
        # enrollment, and they may reasonably expect a new device to appear.
        since = data.get("previously_enrolled_at", "")
        print(
            f"↻ re-bound to existing device {data['device_id']} "
            f"(first enrolled {since or 'previously'}; token rotated, history preserved)"
        )
    else:
        print(f"✓ enrolled as device {data['device_id']} (token written to {args.token_path}, mode 0600)")

    hub_tz = data.get("hub_tz") or args.tz
    for username in local_users:
        policy = data.get("policies", {}).get(username)
        if username in data.get("new_users", []):
            note = (
                "new hub user, policy seeded from this device"
                if local_policies.get(username)
                else "new hub user, hub default policy (1h/day) applied"
            )
        else:
            note = "joined an existing hub user -- pooling with its other device(s)"
        if policy:
            hours = policy["daily_limits_s"][0] / 3600
            print(f"  {username}: hub daily limit {hours:g}h ({note})")
        else:
            print(f"  {username}: {note}")

    config_mod.write_env_file(
        hub_url=hub_url, managed_users=",".join(local_users), tz=hub_tz, ca_cert=args.ca_cert
    )
    print(f"✓ config written to {config_mod.DEFAULT_ENV_PATH}")

    if not args.no_start:
        try:
            # `enable --now` is a no-op on an *already-running* unit -- it
            # only ensures the unit is enabled and started, neither of
            # which changes for a unit that's already both. That silently
            # orphaned the token this enroll just wrote: a re-enroll while
            # the service was already active (e.g. a rebind after a
            # reinstall) left the running process holding the OLD token in
            # memory (HubClient loads it once, at __init__) while the DB
            # now expects the new one, and every subsequent /sync 401'd --
            # which the agent treats as DeviceRevokedError and enters
            # `closed` enforcement immediately, i.e. the child looks locked
            # out for no reason even though the hub thinks everything is
            # fine. `enable` (idempotent, no restart) followed by an
            # unconditional `restart` (starts a stopped unit, restarts a
            # running one) covers both the first-ever enroll and every
            # re-enroll after it with the same two commands.
            subprocess.run(["systemctl", "enable", SERVICE_UNIT], check=True)
            subprocess.run(["systemctl", "restart", SERVICE_UNIT], check=True)
            print(f"✓ {SERVICE_UNIT} enabled and (re)started")
        except (OSError, subprocess.CalledProcessError) as exc:
            print(f"! could not enable/restart the service automatically ({exc}); run:")
            print(f"    sudo systemctl enable --now {SERVICE_UNIT}")
            print(f"    sudo systemctl restart {SERVICE_UNIT}")


def _cmd_run(args: argparse.Namespace) -> None:
    hub = HubClient(
        HubClientConfig(base_url=args.hub_url, token_path=Path(args.token_path), ca_cert=args.ca_cert)
    )
    state_path = Path(args.state_path)
    state = state_mod.load(state_path)
    managed_users = [u.strip() for u in args.users.split(",") if u.strip()]

    # Never exit on a transient condition -- timekprd not up yet, the hub
    # unreachable, or (before the first successful enroll+config) missing
    # settings altogether. Restart=always would bring the process back
    # anyway, but that's a 10s outage window on every blip for no reason;
    # looping here means the *next* tick just works once the transient
    # condition clears. Only a genuinely unrecoverable setup problem
    # (nothing here currently raises one after argparse) should exit.
    enforcer: TimekprEnforcer | None = None
    ready_sent = False
    while enforcer is None:
        try:
            enforcer = TimekprEnforcer()
        except TimekprNotFoundError as exc:
            log.error("%s -- retrying in 30s", exc)
            time.sleep(30)

    while True:
        next_poll_ms = run_tick(
            enforcer=enforcer,
            hub=hub,
            state=state,
            managed_users=managed_users,
            agent_version=AGENT_VERSION,
            tz_name=args.tz,
        )
        state_mod.save(state, state_path)
        if not ready_sent:
            # First tick has completed (whether or not it reached the hub --
            # that's exactly what the watchdog/offline handling is for), so
            # systemd can stop waiting and consider the unit started.
            sd_notify("READY=1")
            ready_sent = True
        sd_notify("WATCHDOG=1")
        if args.once:
            break
        time.sleep(next_poll_ms / 1000)


def _check(label: str, ok: bool, detail: str = "") -> bool:
    mark = "✓" if ok else "✗"
    print(f"{mark} {label}" + (f" -- {detail}" if detail and not ok else ""))
    return ok


def _cmd_status(args: argparse.Namespace) -> None:
    """`timekpr-hub-agent status`: one line per check, so a parent (or this
    agent's own `run` at startup) can see exactly which link in the chain
    is broken instead of a bare "it's not working"."""
    all_ok = True

    try:
        enforcer = TimekprEnforcer()
        all_ok &= _check("timekpr-next installed", True)
    except TimekprNotFoundError as exc:
        _check("timekpr-next installed", False, str(exc))
        enforcer = None
        all_ok = False

    if enforcer is not None:
        connected = enforcer.connect()
        all_ok &= _check(
            "timekprd reachable over DBUS", connected, "check group membership / timekprd status"
        )

    env_values = config_mod.read_env_file()
    hub_url = config_mod.env_default("TIMEKPR_HUB_URL", env_values)
    all_ok &= _check(
        "config present", bool(hub_url), f"run `timekpr-hub-agent enroll` ({config_mod.DEFAULT_ENV_PATH})"
    )

    token_path = Path(args.token_path)
    token_ok = token_path.exists() and os.access(token_path, os.R_OK)
    all_ok &= _check("device token present and readable", token_ok, str(token_path))

    try:
        enabled = subprocess.run(
            ["systemctl", "is-enabled", SERVICE_UNIT], capture_output=True, text=True, check=False
        ).stdout.strip()
        active = subprocess.run(
            ["systemctl", "is-active", SERVICE_UNIT], capture_output=True, text=True, check=False
        ).stdout.strip()
        all_ok &= _check(f"service enabled ({enabled or 'unknown'})", enabled == "enabled")
        all_ok &= _check(f"service active ({active or 'unknown'})", active == "active")
    except OSError:
        _check("service enabled/active", False, "systemctl not available")

    if hub_url:
        try:
            hub = HubClient(
                HubClientConfig(
                    base_url=hub_url,
                    token_path=token_path,
                    ca_cert=config_mod.env_default("TIMEKPR_HUB_CA_CERT", env_values),
                )
            )
            hub.sync(
                {
                    "agent_time": datetime.now(UTC).isoformat(),
                    "tz": config_mod.env_default("TIMEKPR_HUB_TZ", env_values) or "UTC",
                    "ntp_synced": True,
                    "agent_version": AGENT_VERSION,
                    "users": [],
                }
            )
            all_ok &= _check("hub reachable", True)
        except (HubUnreachableError, DeviceRevokedError) as exc:
            all_ok &= _check("hub reachable", False, str(exc))

    state = state_mod.load(Path(args.state_path))
    managed_users = [
        u.strip()
        for u in (config_mod.env_default("TIMEKPR_HUB_MANAGED_USERS", env_values) or "").split(",")
        if u.strip()
    ]
    for username in managed_users:
        user_state = state.users.get(username)
        if user_state is None:
            print(f"  {username}: no sync recorded yet")
            continue
        last_sync = (
            datetime.fromtimestamp(user_state.last_hub_contact_utc, UTC).isoformat()
            if user_state.last_hub_contact_utc
            else "never"
        )
        print(
            f"  {username}: last sync {last_sync}, global spent {user_state.last_global_spent_s}s / "
            f"limit {user_state.last_effective_limit_today_s}s, policy v{user_state.policy_version_applied}, "
            f"enforcement={user_state.last_enforcement or 'unknown'}"
        )

    sys.exit(0 if all_ok else 1)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    env_values = config_mod.read_env_file()

    parser = argparse.ArgumentParser(prog="timekpr-hub-agent")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="run the tick loop against an enrolled hub")
    _add_hub_connection_args(run_parser, env_values)
    run_parser.add_argument(
        "--users",
        default=config_mod.env_default("TIMEKPR_HUB_MANAGED_USERS", env_values),
        required=config_mod.env_default("TIMEKPR_HUB_MANAGED_USERS", env_values) is None,
        help="comma-separated list of local usernames to manage",
    )
    run_parser.add_argument("--tz", default=config_mod.env_default("TIMEKPR_HUB_TZ", env_values) or "UTC")
    run_parser.add_argument("--state-path", default=str(state_mod.DEFAULT_STATE_PATH))
    run_parser.add_argument("--once", action="store_true", help="run a single tick and exit (for testing)")
    run_parser.set_defaults(func=_cmd_run)

    enroll_parser = subparsers.add_parser(
        "enroll", help="redeem an enrollment code, store the token, write config, and start the service"
    )
    _add_hub_connection_args(enroll_parser, env_values, prompt_if_missing=True)
    enroll_parser.add_argument(
        "--code", default=None, help="one-time enrollment code from the hub (prompted if omitted)"
    )
    enroll_parser.add_argument(
        "--users",
        default=None,
        help="comma-separated local usernames this device reports (prompted interactively if omitted)",
    )
    enroll_parser.add_argument("--tz", default="UTC", help="overridden by the hub's HUB_TZ once enrolled")
    enroll_parser.add_argument("--hostname", default=None, help="default: this machine's hostname")
    enroll_parser.add_argument("--machine-id", default=None, help="default: /etc/machine-id")
    enroll_parser.add_argument("--os", default="linux")
    enroll_parser.add_argument(
        "--no-start", action="store_true", help="don't run `systemctl enable --now` after enrolling"
    )
    enroll_parser.set_defaults(func=_cmd_enroll)

    status_parser = subparsers.add_parser("status", help="check every link in the chain, one line per check")
    status_parser.add_argument("--token-path", default=str(DEFAULT_TOKEN_PATH))
    status_parser.add_argument("--state-path", default=str(state_mod.DEFAULT_STATE_PATH))
    status_parser.set_defaults(func=_cmd_status)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
