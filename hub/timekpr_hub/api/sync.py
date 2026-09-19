"""POST /sync -- the workhorse endpoint.

One call covers every managed user on a device. For each user, the hub:
  1. records the wall-clock activity span and the absolute cumulative
     counter reported by the agent (both idempotent -- see
     `services/aggregate.py`),
  2. recomputes the global spent-today total (wallclock union or parallel
     sum, per the user's `accounting_mode`),
  3. returns the effective limit and global spent so the agent's
     `timekpr_hub_core.convergence.plan()` can decide what (if anything) to
     write back to the local timekpr daemon.

The hub does NOT run the convergence algorithm itself -- that stays on the
agent, next to the DBUS call it drives. The agent is deliberately dumb:
measure -> report -> receive a target -> nudge the balance.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from timekpr_hub_core.calendar import canonical_stamp, days_in_iso_week, month_bounds
from timekpr_hub_core.models import (
    EnforcementMode,
    OfflinePolicy,
    SyncRequest,
    SyncResponse,
    SyncUserResponse,
)

from timekpr_hub.api.auth import get_current_device
from timekpr_hub.db.models import Device, User, UserAlias
from timekpr_hub.db.session import get_session
from timekpr_hub.services.aggregate import (
    global_spent_parallel,
    global_spent_parallel_window,
    global_spent_wallclock,
    global_spent_wallclock_window,
    insert_activity_interval,
    upsert_usage_counter,
)
from timekpr_hub.services.enrollment import provision_user_alias
from timekpr_hub.services.limits import effective_daily_limit
from timekpr_hub.services.policy import effective_policy_payload, get_or_create_policy
from timekpr_hub.settings import settings

router = APIRouter()


@router.post("/sync", response_model=SyncResponse)
async def sync(
    req: SyncRequest,
    device: Device = Depends(get_current_device),
    session: AsyncSession = Depends(get_session),
) -> SyncResponse:
    now = datetime.now(UTC)
    stamp = canonical_stamp(now, settings.tz)

    # `timekpr-hub-agent status` probes connectivity with an empty `users`
    # list -- treat that as a pure connectivity check, not a real check-in,
    # so running `status` on a dead agent's machine can't make it look
    # freshly synced.
    if req.users:
        device.last_seen_at = now
        device.last_sync_at = now
        device.agent_version = req.agent_version
        # Both operands are the same instant, one measured by the agent's
        # clock and one by the hub's -- their difference is how far the
        # device's clock has drifted. This matters beyond diagnostics:
        # activity spans are timestamped entirely from the agent's own
        # clock (main.py) and inserted into activity_intervals verbatim, so
        # a skewed device's spans land at the wrong wall-clock position
        # relative to every other device's -- simultaneous use on two
        # devices can then fail to overlap in the union query, and "burn
        # once" silently becomes "burn twice".
        device.clock_skew_ms = round((now - datetime.fromisoformat(req.agent_time)).total_seconds() * 1000)

    user_responses: list[SyncUserResponse] = []

    for user_sync in req.users:
        alias_result = await session.execute(
            select(UserAlias).where(
                UserAlias.device_id == device.id, UserAlias.local_username == user_sync.username
            )
        )
        alias = alias_result.scalar_one_or_none()
        if alias is None:
            # A local username this device hasn't reported before -- e.g. a
            # second local account added to the agent's managed list after
            # enrollment. The device's own bearer token is exactly the
            # authorization enroll's `local_users` already relies on, so
            # provisioning here rather than staying observe-only forever
            # means adding a user to an enrolled machine needs no
            # revoke/re-enroll round trip; the very same sync then falls
            # through to full enforcement below rather than needing a
            # second tick.
            user, policy, _ = await provision_user_alias(
                session, device_id=device.id, local_username=user_sync.username
            )
        else:
            user_result = await session.execute(select(User).where(User.id == alias.user_id))
            user = user_result.scalar_one()

            # SELECT ... FOR UPDATE-guarded against two devices reaching
            # this user's first sync concurrently (services/policy.py's
            # get_or_create_policy docstring).
            policy = await get_or_create_policy(session, user)

        # 1. record this tick's contribution (idempotent on both writes)
        await upsert_usage_counter(
            session,
            user_id=user.id,
            device_id=device.id,
            day=stamp.day,
            spent_seconds=user_sync.cumulative_spent_s,
            activity_state=user_sync.observed.activity_state.value,
        )
        for span in user_sync.active_spans:
            # Each span is inserted independently and idempotently on
            # (device_id, window_end_ts) -- normally exactly one (this
            # tick's), but replayed buffered spans from an outage the agent
            # couldn't reach the hub with (main.py's pending_spans) land here
            # too, and re-inserting an already-recorded one is a no-op.
            start = datetime.fromisoformat(span.start)
            end = datetime.fromisoformat(span.end)
            if end <= start:
                # Malformed/zero-width span (clock oddity, bad buffering) --
                # skip rather than let tstzrange or the union computation
                # choke on it -- unvalidated input here can 500.
                continue
            await insert_activity_interval(
                session,
                user_id=user.id,
                device_id=device.id,
                day=stamp.day,
                start=start,
                end=end,
                window_end_ts=end,
            )

        # 2. recompute the global total per this user's accounting mode
        wallclock = user.accounting_mode == "wallclock"
        if wallclock:
            global_spent = await global_spent_wallclock(session, user_id=user.id, day=stamp.day)
        else:
            global_spent = await global_spent_parallel(session, user_id=user.id, day=stamp.day)

        # 3. effective limits -- daily is the full policy+grants+overrides+gate
        # combiner; week/month pool the SAME accounting mode's spend across
        # their own window and subtract it from the policy's standing
        # ceiling, so a device never sees a week/month limit more permissive
        # than what's actually left in the pool.
        limit_today = await effective_daily_limit(session, policy=policy, user=user, day=stamp.day)

        week_start, week_end = days_in_iso_week(stamp.day)[0], days_in_iso_week(stamp.day)[-1]
        month_start, month_end = month_bounds(stamp.day)
        window_spend = global_spent_wallclock_window if wallclock else global_spent_parallel_window
        week_spent = await window_spend(session, user_id=user.id, start_day=week_start, end_day=week_end)
        month_spent = await window_spend(session, user_id=user.id, start_day=month_start, end_day=month_end)
        week_limit = max(0, policy.weekly_limit_s - week_spent)
        month_limit = max(0, policy.monthly_limit_s - month_spent)

        enforcement = EnforcementMode.OBSERVE if device.enforcement == "observe" else EnforcementMode.ENFORCE

        # Two branches, gated on whether THIS agent has ever echoed a
        # `policy_revision_applied` at all (see that field's docstring in
        # core/timekpr_hub_core/models.py). A legacy agent only ever reports
        # back the int policy version, so it must be served the standing
        # payload and gated on that version alone -- exactly today's
        # behavior -- because it can never be told to revert a one-day
        # hours override (an expiring override does not change
        # `policy.version`). Any agent that HAS reported a revision (even
        # an empty one, on its very first tick) gets the effective payload
        # -- standing policy plus today's hours override, if any -- gated
        # on the revision, which changes exactly when what belongs on the
        # device changes (see timekpr_hub_core.effective_policy).
        payload, revision = await effective_policy_payload(
            session,
            policy=policy,
            day=stamp.day,
            apply_day_overrides=user_sync.policy_revision_applied is not None,
        )
        if user_sync.policy_revision_applied is not None:
            policy_payload = payload if user_sync.policy_revision_applied != revision else None
        else:
            policy_payload = payload if user_sync.policy_version_applied != policy.version else None

        user_responses.append(
            SyncUserResponse(
                username=user_sync.username,
                global_spent_s=global_spent,
                effective_limit_today_s=limit_today,
                effective_week_limit_s=week_limit,
                effective_month_limit_s=month_limit,
                enforcement=enforcement,
                policy_version=policy.version,
                policy_revision=revision,
                policy=policy_payload,
                offline_policy=OfflinePolicy(user.offline_policy),
                offline_grace_s=user.offline_grace_s,
                offline_cap_s=user.offline_cap_s,
            )
        )

    await session.commit()

    return SyncResponse(
        hub_time=now.isoformat(),
        hub_tz=settings.hub_tz,
        day=stamp.day_str,
        iso_week=stamp.iso_week_str,
        month=stamp.month_str,
        next_poll_ms=settings.default_next_poll_ms,
        users=user_responses,
    )
