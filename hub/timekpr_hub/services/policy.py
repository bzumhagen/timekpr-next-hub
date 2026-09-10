"""Policy lookup/versioning helpers.

PLAN reference: "Policy push, and the 'a parent edited it locally' problem".
Phase 1 scope: fetch the current policy version and payload for a user; full
field-by-field diffing/adoption workflow is Phase 2 (see CHECKLIST.md).
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from timekpr_hub_core.models import PolicyPayload

from timekpr_hub.db.models import Policy, User

DEFAULT_DAILY_LIMITS_S = [3600] * 7  # 1h/day default for a newly-created user, all days


async def get_current_policy(session: AsyncSession, user: User) -> Policy | None:
    if user.current_policy_id is None:
        return None
    result = await session.execute(select(Policy).where(Policy.id == user.current_policy_id))
    return result.scalar_one_or_none()


def policy_to_payload(policy: Policy) -> PolicyPayload:
    return PolicyPayload(
        version=policy.version,
        daily_limits_s=policy.daily_limits_json,
        allowed_hours=policy.allowed_hours_json or {},
        allowed_weekdays=policy.allowed_weekdays_json or [],
        weekly_limit_s=policy.weekly_limit_s,
        monthly_limit_s=policy.monthly_limit_s,
        lockout_type=policy.lockout_type,
        wake_from=policy.wake_from,
        wake_to=policy.wake_to,
        track_inactive=policy.track_inactive,
        note=policy.note or "",
    )


async def create_initial_policy(session: AsyncSession, user_id: uuid.UUID) -> Policy:
    """Every newly-registered user needs a policy row before /sync can serve
    them a limit. Phase 1 default: 1h/day, every day, no PlayTime, no
    week/month cap (large placeholder), simple lock on expiry."""
    policy = Policy(
        user_id=user_id,
        version=1,
        created_by="system_default",
        daily_limits_json=DEFAULT_DAILY_LIMITS_S,
        allowed_hours_json={},
        allowed_weekdays_json=["1", "2", "3", "4", "5", "6", "7"],
        weekly_limit_s=sum(DEFAULT_DAILY_LIMITS_S),
        monthly_limit_s=sum(DEFAULT_DAILY_LIMITS_S) * 5,
        lockout_type="lock",
        track_inactive=False,
    )
    session.add(policy)
    await session.flush()
    return policy
