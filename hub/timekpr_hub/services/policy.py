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


async def create_initial_policy(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    daily_limits_s: list[int] | None = None,
    weekly_limit_s: int | None = None,
    monthly_limit_s: int | None = None,
    allowed_weekdays: list[str] | None = None,
) -> Policy:
    """Every newly-registered user needs a policy row before /sync can serve
    them a limit. Default: 1h/day, every day, no PlayTime, no week/month cap
    (large placeholder), simple lock on expiry -- used when no snapshot is
    given (e.g. a user created by hand, or the parent API).

    Phase 5a: `enroll` passes the enrolling device's own currently-configured
    limits here instead, when it has them, so a brand-new hub user's policy
    starts from what's actually running on that device rather than always
    resetting a possibly-already-configured child to the placeholder."""
    limits = daily_limits_s if daily_limits_s is not None else DEFAULT_DAILY_LIMITS_S
    policy = Policy(
        user_id=user_id,
        version=1,
        created_by="system_default" if daily_limits_s is None else "seeded_from_device",
        daily_limits_json=limits,
        allowed_hours_json={},
        allowed_weekdays_json=allowed_weekdays or ["1", "2", "3", "4", "5", "6", "7"],
        weekly_limit_s=weekly_limit_s if weekly_limit_s is not None else sum(limits),
        monthly_limit_s=monthly_limit_s if monthly_limit_s is not None else sum(limits) * 5,
        lockout_type="lock",
        track_inactive=False,
    )
    session.add(policy)
    await session.flush()
    return policy
