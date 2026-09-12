"""Effective limit computation: policy + grants (+ carryover, Phase 2).

PLAN: "effective_limit(u, day) = policy.daily_limits[dow] + Σ grants(u, day)"
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func

from timekpr_hub.db.models import Grant, Policy


def base_daily_limit(policy: Policy, day: date) -> int:
    """`daily_limits_json` is index 0 = Monday .. index 6 = Sunday (ISO
    weekday - 1), matching PLAN's PolicyPayload docstring."""
    return int(policy.daily_limits_json[day.isoweekday() - 1])


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


async def effective_daily_limit(
    session: AsyncSession, *, policy: Policy, user_id: uuid.UUID, day: date
) -> int:
    return base_daily_limit(policy, day) + await grants_total(session, user_id=user_id, day=day)
