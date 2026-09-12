"""Policy lookup/versioning helpers.

PLAN reference: "Policy push, and the 'a parent edited it locally' problem".
Phase 1 scope: fetch the current policy version and payload for a user; full
field-by-field diffing/adoption workflow is Phase 2 (see CHECKLIST.md).
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from timekpr_hub_core.models import PolicyPayload, PolicyUpdate

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


async def update_policy(
    session: AsyncSession, *, user: User, update: PolicyUpdate, created_by: str
) -> Policy:
    """A parent-initiated change: PUT /users/{u}/policy (api/parent.py) and
    the UI's per-day-minutes form both funnel through here. Policies are
    append-only (PLAN "Policy push, and the 'a parent edited it locally'
    problem") -- this always inserts version + 1 and repoints
    `current_policy_id` rather than mutating a row in place, so `sync.py`'s
    `policy_version_applied != policy.version` check picks it up and pushes
    it to every device on their very next tick.

    Only the fields `PolicyUpdate` exposes (daily/weekly/monthly limits,
    allowed weekdays) are settable through this Phase 1 editor;
    allowed_hours/lockout_type/wake window/track_inactive/note carry forward
    from the current policy unchanged, since there's no UI for them yet
    (docs/best-practices-review.md).

    `SELECT ... FOR UPDATE` on the user row for the duration guards against
    two concurrent edits both reading the same current version and racing on
    `uq_policies_user_version` (the same race flagged for enrollment in
    docs/best-practices-review.md, now closed here too)."""
    locked = await session.execute(select(User).where(User.id == user.id).with_for_update())
    locked_user = locked.scalar_one()
    current = await get_current_policy(session, locked_user)
    next_version = (current.version + 1) if current else 1

    policy = Policy(
        user_id=locked_user.id,
        version=next_version,
        created_by=created_by,
        daily_limits_json=update.daily_limits_s,
        allowed_hours_json=current.allowed_hours_json if current else {},
        allowed_weekdays_json=update.allowed_weekdays
        or (current.allowed_weekdays_json if current else ["1", "2", "3", "4", "5", "6", "7"]),
        weekly_limit_s=update.weekly_limit_s,
        monthly_limit_s=update.monthly_limit_s,
        lockout_type=current.lockout_type if current else "lock",
        wake_from=current.wake_from if current else None,
        wake_to=current.wake_to if current else None,
        track_inactive=current.track_inactive if current else False,
        note=current.note if current else None,
    )
    session.add(policy)
    await session.flush()
    locked_user.current_policy_id = policy.id
    return policy
