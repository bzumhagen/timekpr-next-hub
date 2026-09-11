"""POST /sync -- PLAN "API": the workhorse endpoint.

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
agent, next to the DBUS call it drives (PLAN: "The agent is deliberately
dumb: measure -> report -> receive a target -> nudge the balance").
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from timekpr_hub_core.calendar import canonical_stamp
from timekpr_hub_core.models import EnforcementMode, SyncRequest, SyncResponse, SyncUserResponse

from timekpr_hub.api.auth import get_current_device
from timekpr_hub.db.models import Device, User, UserAlias
from timekpr_hub.db.session import get_session
from timekpr_hub.services.aggregate import (
    device_spent_today,
    global_spent_parallel,
    global_spent_wallclock,
    insert_activity_interval,
    upsert_usage_counter,
)
from timekpr_hub.services.limits import effective_daily_limit
from timekpr_hub.services.policy import create_initial_policy, get_current_policy, policy_to_payload
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

    device.last_seen_at = now
    device.last_sync_at = now
    device.agent_version = req.agent_version
    device.tz = req.tz
    device.ntp_synced = req.ntp_synced

    user_responses: list[SyncUserResponse] = []

    for user_sync in req.users:
        alias_result = await session.execute(
            select(UserAlias).where(
                UserAlias.device_id == device.id, UserAlias.local_username == user_sync.username
            )
        )
        alias = alias_result.scalar_one_or_none()
        if alias is None:
            # Unmapped user: observe-only, per PLAN status-code table (409
            # conceptually; we fold it into the response here as an
            # "observe" enforcement so a single /sync call covering several
            # users doesn't have to fail the whole request over one).
            user_responses.append(
                SyncUserResponse(
                    username=user_sync.username,
                    global_spent_s=0,
                    remote_spent_s=0,
                    effective_limit_today_s=0,
                    effective_week_limit_s=0,
                    effective_month_limit_s=0,
                    enforcement=EnforcementMode.OBSERVE,
                    policy_version=0,
                )
            )
            continue

        user_result = await session.execute(select(User).where(User.id == alias.user_id))
        user = user_result.scalar_one()

        policy = await get_current_policy(session, user)
        if policy is None:
            policy = await create_initial_policy(session, user.id)
            user.current_policy_id = policy.id

        # 1. record this tick's contribution (idempotent on both writes)
        await upsert_usage_counter(
            session,
            user_id=user.id,
            device_id=device.id,
            day=stamp.day,
            spent_seconds=user_sync.cumulative_spent_s,
            raw_balance_s=user_sync.observed.balance_s,
            raw_limit_today_s=user_sync.observed.limit_today_s,
        )
        if user_sync.active_span is not None:
            await insert_activity_interval(
                session,
                user_id=user.id,
                device_id=device.id,
                day=stamp.day,
                start=datetime.fromisoformat(user_sync.active_span.start),
                end=datetime.fromisoformat(user_sync.active_span.end),
                window_end_ts=datetime.fromisoformat(user_sync.active_span.end),
            )

        # 2. recompute the global total per this user's accounting mode
        if user.accounting_mode == "wallclock":
            global_spent = await global_spent_wallclock(session, user_id=user.id, day=stamp.day)
        else:
            global_spent = await global_spent_parallel(session, user_id=user.id, day=stamp.day)

        this_device_spent = await device_spent_today(
            session, user_id=user.id, device_id=device.id, day=stamp.day
        )
        remote_spent = max(global_spent - this_device_spent, 0)

        # 3. effective limits
        limit_today = await effective_daily_limit(session, policy=policy, user_id=user.id, day=stamp.day)
        # Week/month pooling is Phase 3 (PLAN milestone breakdown) -- for now
        # the agent gets the raw policy ceilings, which is a strict superset
        # (never MORE restrictive than intended) of the eventual behavior.
        week_limit = policy.weekly_limit_s
        month_limit = policy.monthly_limit_s

        enforcement = EnforcementMode.OBSERVE if device.enforcement == "observe" else EnforcementMode.ENFORCE

        policy_payload = (
            policy_to_payload(policy) if user_sync.policy_version_applied != policy.version else None
        )

        user_responses.append(
            SyncUserResponse(
                username=user_sync.username,
                global_spent_s=global_spent,
                remote_spent_s=remote_spent,
                effective_limit_today_s=limit_today,
                effective_week_limit_s=week_limit,
                effective_month_limit_s=month_limit,
                enforcement=enforcement,
                suppressed=False,  # one-active-device-at-a-time is Phase 4
                policy_version=policy.version,
                policy=policy_payload,
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
