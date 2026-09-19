"""One-day allowed-hours overrides (`day_hour_overrides`).

Mirrors `services/limits.py`'s shape for `DayOverride`/`GateRelease`
(reader / `_batch` / `_history` / `set_` via upsert / `clear_`), but lives in
its own module rather than folded into `limits.py`: that module's own
docstring calls itself "the ONE place `effective_limit` is computed" (a
seconds-only formula), and allowed-hours is a different axis entirely --
see `timekpr_hub_core.effective_policy` for why it has to reach the device
at all, unlike everything in `limits.py`.

None of these functions commit; that stays the caller's responsibility, same
as every other write in this neighborhood (`services/policy.py::update_policy`,
`services/limits.py::set_day_override`).
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from timekpr_hub_core.models import AllowedHourInterval

from timekpr_hub.db.models import DayHourOverride


def _dump(intervals: list[AllowedHourInterval]) -> list[dict]:
    return [iv.model_dump(mode="json") for iv in intervals]


def _load(rows: list[dict]) -> list[AllowedHourInterval]:
    return [AllowedHourInterval.model_validate(row) for row in rows]


async def day_hour_override(
    session: AsyncSession, *, user_id: uuid.UUID, day: date
) -> DayHourOverride | None:
    result = await session.execute(
        select(DayHourOverride).where(DayHourOverride.user_id == user_id, DayHourOverride.day == day)
    )
    return result.scalar_one_or_none()


async def day_hour_overrides_batch(
    session: AsyncSession, *, user_ids: list[uuid.UUID], day: date
) -> dict[uuid.UUID, list[AllowedHourInterval]]:
    """One lookup for every user in `user_ids`, mirroring
    `limits.py::day_overrides_batch`'s shape. A user with no override that
    day is simply absent; callers should fall back to the policy's standing
    hours for that weekday."""
    if not user_ids:
        return {}
    result = await session.execute(
        select(DayHourOverride.user_id, DayHourOverride.intervals_json).where(
            DayHourOverride.user_id.in_(user_ids), DayHourOverride.day == day
        )
    )
    return {user_id: _load(intervals_json) for user_id, intervals_json in result.all()}


async def day_hour_overrides_history(
    session: AsyncSession, *, user_id: uuid.UUID, start_day: date, end_day: date
) -> dict[date, list[AllowedHourInterval]]:
    """Every hours override in `[start_day, end_day]` for one user -- the
    stats page's "this day's hours were overridden" annotation, mirroring
    `limits.py::day_overrides_history`'s shape."""
    result = await session.execute(
        select(DayHourOverride.day, DayHourOverride.intervals_json).where(
            DayHourOverride.user_id == user_id,
            DayHourOverride.day >= start_day,
            DayHourOverride.day <= end_day,
        )
    )
    return {day: _load(intervals_json) for day, intervals_json in result.all()}


async def set_day_hour_override(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    day: date,
    intervals: list[AllowedHourInterval],
    reason: str,
    created_by: str,
) -> DayHourOverride:
    """Upserts the (user, day) hours override -- setting one twice for the
    same date replaces it, same "change my mind" reasoning as
    `limits.py::set_day_override`. `intervals` must be non-empty (an empty
    list means "forbidden all day" to timekpr, never "unrestricted" -- see
    `timekpr_hub_core.allowed_hours.unrestricted`); callers pass
    `intervals_to_hours(unrestricted())`'s wire equivalent for the "any time"
    mode, never `[]`. The DB's own check constraint
    (`ck_day_hour_overrides_nonempty`) is the backstop if a caller forgets."""
    insert_stmt = pg_insert(DayHourOverride).values(
        id=uuid.uuid4(),
        user_id=user_id,
        day=day,
        intervals_json=_dump(intervals),
        reason=reason,
        created_by=created_by,
    )
    upsert_stmt = insert_stmt.on_conflict_do_update(
        index_elements=[DayHourOverride.user_id, DayHourOverride.day],
        set_={
            "intervals_json": insert_stmt.excluded.intervals_json,
            "reason": insert_stmt.excluded.reason,
            "created_by": insert_stmt.excluded.created_by,
        },
    ).returning(DayHourOverride)
    result = await session.execute(upsert_stmt)
    return result.scalar_one()


async def clear_day_hour_override(session: AsyncSession, *, user_id: uuid.UUID, day: date) -> bool:
    """Removes an hours override for one date, if any. Returns whether a row
    was actually deleted, so a caller can tell "cleared" from "there was
    nothing to clear" without a separate lookup."""
    existing = await day_hour_override(session, user_id=user_id, day=day)
    if existing is None:
        return False
    await session.delete(existing)
    return True
