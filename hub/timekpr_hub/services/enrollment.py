"""Provisioning a local username onto a hub `User` -- shared by `/enroll`
(a device's initial batch of local users) and `/sync` (a local user added to
an already-enrolled machine's managed list later, which previously had no
way to ever become a real hub user -- see api/sync.py's unmapped-user
branch)."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from timekpr_hub_core.models import LocalPolicySnapshot

from timekpr_hub.db.models import Policy, User, UserAlias
from timekpr_hub.services.policy import create_initial_policy, get_or_create_policy


async def provision_user_alias(
    session: AsyncSession,
    *,
    device_id: uuid.UUID,
    local_username: str,
    local_policy_snapshot: LocalPolicySnapshot | None = None,
) -> tuple[User, Policy, bool]:
    """Provision (or merge into) the canonical `User` for `local_username`,
    point a `UserAlias` at it from `device_id`, and return `(user, policy,
    is_new)`. `is_new` is True only when this call created a brand-new hub
    user -- callers use it to decide whether to report the seeded policy
    back (enroll's `new_users`) and whether `local_policy_snapshot` applies
    at all (it's ignored when merging into an existing user).

    Savepoint-guarded so a unique-constraint race against a concurrent
    provision of the same brand-new username (two devices, or a device's
    enroll racing its own first sync) falls back to "someone else just
    created it" instead of aborting the caller's whole transaction.
    """
    is_new = False
    try:
        async with session.begin_nested():
            user = User(id=uuid.uuid4(), canonical_username=local_username, display_name=local_username)
            session.add(user)
            await session.flush()
        is_new = True
    except IntegrityError:
        existing = await session.execute(select(User).where(User.canonical_username == local_username))
        user = existing.scalar_one()

    if is_new:
        # Seed the initial policy from this device's own configured limits
        # when it reported one, rather than always falling back to the
        # hub's 1h/day placeholder -- a brand-new user whose only device
        # already has, say, a 2h/day limit configured shouldn't suddenly
        # show 1h/day in the hub UI.
        policy = await create_initial_policy(
            session,
            user.id,
            daily_limits_s=local_policy_snapshot.daily_limits_s if local_policy_snapshot else None,
            weekly_limit_s=local_policy_snapshot.weekly_limit_s if local_policy_snapshot else None,
            monthly_limit_s=local_policy_snapshot.monthly_limit_s if local_policy_snapshot else None,
            allowed_weekdays=local_policy_snapshot.allowed_weekdays if local_policy_snapshot else None,
        )
        user.current_policy_id = policy.id
    else:
        # SELECT ... FOR UPDATE-guarded against two devices provisioning the
        # same brand-new (to the hub) existing user concurrently, each
        # seeing current_policy_id is None before the other's create
        # commits (services/policy.py's get_or_create_policy docstring).
        policy = await get_or_create_policy(session, user)

    # The alias insert (FK'd to user_id) must come AFTER the FOR UPDATE lock
    # above, not before it -- this order was originally reversed and
    # produced a genuine Postgres deadlock under exactly this concurrent-
    # enroll scenario (see tests/integration/test_hub_api.py's
    # test_concurrent_first_policy_creation_for_a_shared_user_does_not_race):
    # each transaction's INSERT INTO user_aliases first takes a shared (FOR
    # KEY SHARE) lock on the referenced `users` row to validate the FK, then
    # get_or_create_policy's SELECT ... FOR UPDATE tries to upgrade that
    # SAME row to an exclusive lock -- two transactions both holding the
    # shared lock and both waiting on each other's to release before their
    # own upgrade can proceed is a textbook deadlock. Acquiring the
    # exclusive FOR UPDATE lock first means only one transaction ever holds
    # any lock on the row at a time; the other blocks cleanly instead of
    # deadlocking.
    alias_stmt = pg_insert(UserAlias).values(
        id=uuid.uuid4(), user_id=user.id, device_id=device_id, local_username=local_username
    )
    alias_stmt = alias_stmt.on_conflict_do_nothing(
        index_elements=[UserAlias.device_id, UserAlias.local_username]
    )
    await session.execute(alias_stmt)

    return user, policy, is_new
