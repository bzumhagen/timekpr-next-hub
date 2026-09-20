"""Policy lookup/versioning helpers: fetch the current policy version and
payload for a user, and append a new version when an admin edits it.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from timekpr_hub_core.effective_policy import (
    materialize_allowed_hours,
    policy_revision,
    with_day_hour_override,
)
from timekpr_hub_core.models import (
    WEEKDAY_TOKENS,
    AllowedHourInterval,
    PlayTimeActivity,
    PlayTimePayload,
    PolicyPayload,
    PolicyUpdate,
)

from timekpr_hub.db.models import Policy, User
from timekpr_hub.services.day_hours import day_hour_override

DEFAULT_DAILY_LIMITS_S = [3600] * 7  # 1h/day default for a newly-created user, all days


async def get_current_policy(session: AsyncSession, user: User) -> Policy | None:
    if user.current_policy_id is None:
        return None
    result = await session.execute(select(Policy).where(Policy.id == user.current_policy_id))
    return result.scalar_one_or_none()


def policy_to_payload(policy: Policy) -> PolicyPayload:
    """Faithful to storage -- deliberately NOT materialized (see
    `timekpr_hub_core.effective_policy.materialize_allowed_hours`) and
    deliberately NOT day-hour-override-aware. This feeds the enroll-time
    diff and the audit log's before/after blobs, both of which need to show
    what was actually saved, not a 7-day-expanded, override-applied
    projection of it. `effective_policy_payload` below is the function that
    adds both of those for `/sync`'s benefit."""
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
        hide_tray_icon=policy.hide_tray_icon,
        playtime=PlayTimePayload(
            enabled=policy.playtime_enabled,
            override_enabled=policy.playtime_override_enabled,
            unaccounted_intervals_enabled=policy.playtime_unaccounted_intervals_enabled,
            allowed_weekdays=policy.playtime_allowed_weekdays_json or [],
            daily_limits_s=policy.playtime_daily_limits_json or [0] * 7,
            activities=[PlayTimeActivity(**a) for a in (policy.playtime_activities_json or [])],
        ),
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
    given (e.g. a user created by hand, or the admin API).

    `enroll` passes the enrolling device's own currently-configured limits
    here instead, when it has them, so a brand-new hub user's policy
    starts from what's actually running on that device rather than always
    resetting a possibly-already-configured user to the placeholder."""
    limits = daily_limits_s if daily_limits_s is not None else DEFAULT_DAILY_LIMITS_S
    policy = Policy(
        user_id=user_id,
        version=1,
        created_by="system_default" if daily_limits_s is None else "seeded_from_device",
        daily_limits_json=limits,
        allowed_hours_json={},
        allowed_weekdays_json=allowed_weekdays or list(WEEKDAY_TOKENS),
        weekly_limit_s=weekly_limit_s if weekly_limit_s is not None else sum(limits),
        monthly_limit_s=monthly_limit_s if monthly_limit_s is not None else sum(limits) * 5,
        lockout_type="lock",
        track_inactive=False,
    )
    session.add(policy)
    await session.flush()
    return policy


async def get_or_create_policy(
    session: AsyncSession,
    user: User,
    *,
    daily_limits_s: list[int] | None = None,
    weekly_limit_s: int | None = None,
    monthly_limit_s: int | None = None,
    allowed_weekdays: list[str] | None = None,
) -> Policy:
    """Returns `user`'s current policy, creating the default (or a
    device-seeded, via the `daily_limits_s`/etc. kwargs) one first if none
    exists yet. Callers: `/sync`'s first-tick-for-a-user path and `/enroll`'s
    "existing user, no policy yet" path -- both can be raced by two devices
    reaching the same brand-new-to-the-hub user concurrently, each reading
    `current_policy_id is None` before the other's insert commits, and both
    then racing to insert version 1 (`uq_policies_user_version`).
    `SELECT ... FOR UPDATE` on the user row serializes the read-then-create
    the same way `update_policy` below already does for policy edits."""
    locked = await session.execute(
        select(User).where(User.id == user.id).with_for_update().execution_options(populate_existing=True)
    )
    locked_user = locked.scalar_one()
    current = await get_current_policy(session, locked_user)
    if current is not None:
        return current
    policy = await create_initial_policy(
        session,
        locked_user.id,
        daily_limits_s=daily_limits_s,
        weekly_limit_s=weekly_limit_s,
        monthly_limit_s=monthly_limit_s,
        allowed_weekdays=allowed_weekdays,
    )
    locked_user.current_policy_id = policy.id
    return policy


async def update_policy(
    session: AsyncSession, *, user: User, update: PolicyUpdate, created_by: str
) -> Policy:
    """An admin-initiated change: PUT /users/{u}/policy (api/admin/users.py) and
    the UI's basic/advanced policy forms both funnel through here. Policies
    are append-only -- this always inserts version + 1 and repoints
    `current_policy_id` rather than mutating a row in place, so `sync.py`'s
    `policy_version_applied != policy.version` check picks it up and pushes
    it to every device on their very next tick.

    Every field `PolicyUpdate` exposes is written here -- no field is
    carried forward from the current policy unedited, since the advanced
    editor now covers all of them (allowed_hours/lockout_type/wake
    window/track_inactive/hide_tray_icon/PlayTime/note included). A caller
    that wants to change only one field must still submit the whole
    `PolicyUpdate`, seeded from the current policy's payload -- the same
    "full form, one save" shape the UI presents.

    `SELECT ... FOR UPDATE` on the user row for the duration guards against
    two concurrent edits both reading the same current version and racing on
    `uq_policies_user_version` (the same race enrollment guards against).
    `populate_existing`
    is required, not cosmetic: every caller here already loaded `user` once
    earlier in this same session (to resolve the username), so without it
    SQLAlchemy's identity map would hand back that same Python object,
    stale `current_policy_id` and all, once the lock is granted --
    defeating the whole point of re-reading under the lock."""
    locked = await session.execute(
        select(User).where(User.id == user.id).with_for_update().execution_options(populate_existing=True)
    )
    locked_user = locked.scalar_one()
    current = await get_current_policy(session, locked_user)
    next_version = (current.version + 1) if current else 1
    pt = update.playtime

    policy = Policy(
        user_id=locked_user.id,
        version=next_version,
        created_by=created_by,
        daily_limits_json=update.daily_limits_s,
        allowed_hours_json={
            day: [h.model_dump() for h in hours] for day, hours in update.allowed_hours.items()
        },
        allowed_weekdays_json=update.allowed_weekdays,
        weekly_limit_s=update.weekly_limit_s,
        monthly_limit_s=update.monthly_limit_s,
        lockout_type=update.lockout_type.value,
        wake_from=update.wake_from,
        wake_to=update.wake_to,
        track_inactive=update.track_inactive,
        hide_tray_icon=update.hide_tray_icon,
        playtime_enabled=pt.enabled,
        playtime_override_enabled=pt.override_enabled,
        playtime_unaccounted_intervals_enabled=pt.unaccounted_intervals_enabled,
        playtime_allowed_weekdays_json=pt.allowed_weekdays,
        playtime_daily_limits_json=pt.daily_limits_s,
        playtime_activities_json=[a.model_dump() for a in pt.activities],
        note=update.note or None,
    )
    session.add(policy)
    await session.flush()
    locked_user.current_policy_id = policy.id
    return policy


async def effective_policy_payload(
    session: AsyncSession, *, policy: Policy, day: date, apply_day_overrides: bool = True
) -> tuple[PolicyPayload, str]:
    """The payload `/sync` actually decides whether to push, plus the
    revision token that decision is gated on -- see
    `timekpr_hub_core.effective_policy`'s module docstring for why this
    can't just be `policy_to_payload` + `policy.version` once a one-day
    hours override exists.

    `day` is an explicit parameter rather than read from the clock inside,
    specifically so a caller (a test, or a future "preview tomorrow" view)
    can ask what the payload would be for any date without touching the
    system clock -- `/sync` itself always passes its own server-computed
    `stamp.day` and must keep doing so (see
    `tests/integration/test_gates_and_overrides.py`'s warning that `/sync`
    does not trust the request body for "today").

    `apply_day_overrides=False` gets the always-materialized standing
    payload (7 full weekday keys, no per-date substitution) -- what a
    legacy agent (one that predates `policy_revision_applied`) must be
    served, since it can only ever be told to revert via the policy
    *version*, which an expiring hours override does not change."""
    standing = policy_to_payload(policy)
    materialized_hours = materialize_allowed_hours(standing.allowed_hours)
    materialized = standing.model_copy(update={"allowed_hours": materialized_hours})

    if not apply_day_overrides:
        # A legacy agent (see `SyncUserRequest.policy_revision_applied`'s
        # docstring): the materialized-but-not-overridden standing payload,
        # gated on `policy.version` alone exactly as before this feature.
        return materialized, f"{materialized.version}-legacy"

    override = await day_hour_override(session, user_id=policy.user_id, day=day)
    if override is None:
        return materialized, policy_revision(materialized)

    effective = with_day_hour_override(
        materialized,
        weekday=str(day.isoweekday()),
        intervals=[AllowedHourInterval.model_validate(iv) for iv in override.intervals_json],
    )
    return effective, policy_revision(effective)
