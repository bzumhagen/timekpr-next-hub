"""Effective limit computation: policy + grants + per-date overrides + the
chore gate (+ carryover, Phase 2 future work).

PLAN: "effective_limit(u, day) = policy.daily_limits[dow] + Σ grants(u, day)",
extended by `combine_limit` below to also apply a per-date `DayOverride`
(replaces the base outright) and the chore gate (`users.gated_weekdays_json` +
`GateRelease`, forces the day to 0 until released).

`combine_limit` is the ONE place this formula is computed. It used to be
duplicated three times -- here, in services/summaries.py's batched dashboard
path, and in its usage-history path -- agreeing only by coincidence
(docs/best-practices-review.md-style finding). All three now call this
function so a gated or overridden day reads the same everywhere: the
dashboard, the stats page, and `/sync`'s actual enforcement.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func

from timekpr_hub.db.models import DayOverride, GateRelease, Grant, Policy, User


def base_daily_limit(policy: Policy, day: date) -> int:
    """`daily_limits_json` is index 0 = Monday .. index 6 = Sunday (ISO
    weekday - 1), matching PLAN's PolicyPayload docstring."""
    return int(policy.daily_limits_json[day.isoweekday() - 1])


def is_gated_weekday(user: User, day: date) -> bool:
    """Pure: is `day`'s weekday one of this user's `gated_weekdays_json`?
    Says nothing about whether it's been *released* -- see `combine_limit`."""
    return str(day.isoweekday()) in (user.gated_weekdays_json or [])


def combine_limit(*, base_s: int, grants_s: int, gated: bool, released: bool) -> int:
    """The one formula. `base_s` is either the policy's standing limit for
    that weekday or a `DayOverride.limit_seconds` if one exists for that
    date (the override REPLACES the base, per-date grants still ADD on top
    of either). A gated-and-unreleased day is forced to 0 regardless of
    base/grants -- login is still allowed (see README/plan: this withholds
    time, not access), the agent just converges to zero time left."""
    if gated and not released:
        return 0
    return max(0, base_s + grants_s)


# --------------------------------------------------------------------------
# Grants (existing)
# --------------------------------------------------------------------------


async def grants_total(session: AsyncSession, *, user_id: uuid.UUID, day: date) -> int:
    result = await session.execute(
        select(func.coalesce(func.sum(Grant.seconds), 0)).where(Grant.user_id == user_id, Grant.day == day)
    )
    return int(result.scalar_one())


async def grants_totals_batch(
    session: AsyncSession, *, user_ids: list[uuid.UUID], day: date
) -> dict[uuid.UUID, int]:
    """`grants_total` for every user in `user_ids` in one query (`GROUP BY
    user_id`) instead of one query per user -- used by
    services/summaries.py, which previously ran this once per user shown on
    the hub UI (docs/best-practices-review.md's N+1 finding). A user with
    no grants today is simply absent from the result; callers should
    default to 0."""
    if not user_ids:
        return {}
    result = await session.execute(
        select(Grant.user_id, func.sum(Grant.seconds))
        .where(Grant.user_id.in_(user_ids), Grant.day == day)
        .group_by(Grant.user_id)
    )
    return {user_id: int(total) for user_id, total in result.all()}


async def grants_totals_history(
    session: AsyncSession, *, user_id: uuid.UUID, start_day: date, end_day: date
) -> dict[date, int]:
    """`grants_total` for every day in `[start_day, end_day]` in one query --
    the usage-statistics view's "why was Tuesday different" breakdown. A day
    with no grants is simply absent; callers should default to 0."""
    result = await session.execute(
        select(Grant.day, func.sum(Grant.seconds))
        .where(Grant.user_id == user_id, Grant.day >= start_day, Grant.day <= end_day)
        .group_by(Grant.day)
    )
    return {day: int(total) for day, total in result.all()}


# --------------------------------------------------------------------------
# Day overrides ("Tuesday is 30 minutes instead of two hours", 0 = moratorium)
# --------------------------------------------------------------------------


async def day_override(session: AsyncSession, *, user_id: uuid.UUID, day: date) -> DayOverride | None:
    result = await session.execute(
        select(DayOverride).where(DayOverride.user_id == user_id, DayOverride.day == day)
    )
    return result.scalar_one_or_none()


async def day_overrides_batch(
    session: AsyncSession, *, user_ids: list[uuid.UUID], day: date
) -> dict[uuid.UUID, int]:
    """One override lookup for every user in `user_ids`, mirroring
    `grants_totals_batch`'s shape. A user with no override that day is
    simply absent; callers should fall back to the policy's base limit."""
    if not user_ids:
        return {}
    result = await session.execute(
        select(DayOverride.user_id, DayOverride.limit_seconds).where(
            DayOverride.user_id.in_(user_ids), DayOverride.day == day
        )
    )
    return {user_id: int(limit_seconds) for user_id, limit_seconds in result.all()}


async def day_overrides_history(
    session: AsyncSession, *, user_id: uuid.UUID, start_day: date, end_day: date
) -> dict[date, int]:
    """Every override in `[start_day, end_day]` for one user, mirroring
    `grants_totals_history`'s shape -- the stats page's "this day was
    overridden" annotation."""
    result = await session.execute(
        select(DayOverride.day, DayOverride.limit_seconds).where(
            DayOverride.user_id == user_id, DayOverride.day >= start_day, DayOverride.day <= end_day
        )
    )
    return {day: int(limit_seconds) for day, limit_seconds in result.all()}


async def set_day_override(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    day: date,
    limit_seconds: int,
    reason: str,
    created_by: str,
) -> DayOverride:
    """Upserts the (user, day) override -- setting one twice for the same
    date replaces it rather than erroring on `uq_day_overrides_user_day`,
    since "change my mind about tomorrow" is the expected flow, not an edge
    case. Does not commit; caller's responsibility, same as every other
    write in this module's neighborhood (services/policy.py::update_policy)."""
    insert_stmt = pg_insert(DayOverride).values(
        id=uuid.uuid4(),
        user_id=user_id,
        day=day,
        limit_seconds=limit_seconds,
        reason=reason,
        created_by=created_by,
    )
    upsert_stmt = insert_stmt.on_conflict_do_update(
        index_elements=[DayOverride.user_id, DayOverride.day],
        set_={
            "limit_seconds": insert_stmt.excluded.limit_seconds,
            "reason": insert_stmt.excluded.reason,
            "created_by": insert_stmt.excluded.created_by,
        },
    ).returning(DayOverride)
    result = await session.execute(upsert_stmt)
    return result.scalar_one()


async def clear_day_override(session: AsyncSession, *, user_id: uuid.UUID, day: date) -> bool:
    """Removes an override for one date, if any. Returns whether a row was
    actually deleted, so a caller can tell "cleared" from "there was nothing
    to clear" without a separate lookup."""
    existing = await day_override(session, user_id=user_id, day=day)
    if existing is None:
        return False
    await session.delete(existing)
    return True


# --------------------------------------------------------------------------
# Chore gate releases (the per-date exception to `users.gated_weekdays_json`)
# --------------------------------------------------------------------------


async def is_gate_released(session: AsyncSession, *, user_id: uuid.UUID, day: date) -> bool:
    result = await session.execute(
        select(GateRelease.id).where(GateRelease.user_id == user_id, GateRelease.day == day)
    )
    return result.first() is not None


async def gate_releases_batch(
    session: AsyncSession, *, user_ids: list[uuid.UUID], day: date
) -> set[uuid.UUID]:
    """Which users in `user_ids` have released `day`, mirroring the other
    `*_batch` helpers' shape -- one query regardless of how many users are
    shown on the dashboard."""
    if not user_ids:
        return set()
    result = await session.execute(
        select(GateRelease.user_id).where(GateRelease.user_id.in_(user_ids), GateRelease.day == day)
    )
    return {row[0] for row in result.all()}


async def gate_releases_history(
    session: AsyncSession, *, user_id: uuid.UUID, start_day: date, end_day: date
) -> set[date]:
    result = await session.execute(
        select(GateRelease.day).where(
            GateRelease.user_id == user_id, GateRelease.day >= start_day, GateRelease.day <= end_day
        )
    )
    return {row[0] for row in result.all()}


async def release_gate(
    session: AsyncSession, *, user_id: uuid.UUID, day: date, released_by: str, note: str = ""
) -> GateRelease:
    """Upserts the release row -- releasing an already-released day just
    refreshes who/why rather than erroring on the unique constraint."""
    insert_stmt = pg_insert(GateRelease).values(
        id=uuid.uuid4(), user_id=user_id, day=day, released_by=released_by, note=note or None
    )
    upsert_stmt = insert_stmt.on_conflict_do_update(
        index_elements=[GateRelease.user_id, GateRelease.day],
        set_={"released_by": insert_stmt.excluded.released_by, "note": insert_stmt.excluded.note},
    ).returning(GateRelease)
    result = await session.execute(upsert_stmt)
    return result.scalar_one()


async def unrelease_gate(session: AsyncSession, *, user_id: uuid.UUID, day: date) -> bool:
    """Deletes a release row, re-gating that date. Returns whether a row
    actually existed to delete."""
    result = await session.execute(
        select(GateRelease).where(GateRelease.user_id == user_id, GateRelease.day == day)
    )
    existing = result.scalar_one_or_none()
    if existing is None:
        return False
    await session.delete(existing)
    return True


# --------------------------------------------------------------------------
# The single-user entry point `/sync` (api/sync.py) actually calls
# --------------------------------------------------------------------------


async def effective_daily_limit(session: AsyncSession, *, policy: Policy, user: User, day: date) -> int:
    """Takes `user` (not just `user_id`) because it needs
    `gated_weekdays_json` to know whether this day is gated at all -- the
    only call site (api/sync.py) already has the full `User` in scope."""
    override = await day_override(session, user_id=user.id, day=day)
    base_s = override.limit_seconds if override is not None else base_daily_limit(policy, day)
    grants_s = await grants_total(session, user_id=user.id, day=day)
    gated = is_gated_weekday(user, day)
    released = await is_gate_released(session, user_id=user.id, day=day) if gated else False
    return combine_limit(base_s=base_s, grants_s=grants_s, gated=gated, released=released)
