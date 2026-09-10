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


async def effective_daily_limit(
    session: AsyncSession, *, policy: Policy, user_id: uuid.UUID, day: date
) -> int:
    return base_daily_limit(policy, day) + await grants_total(session, user_id=user_id, day=day)
