"""Hub-side aggregation: the MAX-merge of usage_counters and the wall-clock
union of activity_intervals.

PLAN reference: "Idempotency: report absolute counters, never deltas" and
"Wall-clock union (the 'burn once' requirement)". Both queries below were
validated directly against a real Postgres 16 instance during
implementation (see CHECKLIST.md Phase 1 notes) before being wrapped here.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from timekpr_hub.db.models import ActivityInterval, UsageCounter


async def upsert_usage_counter(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    device_id: uuid.UUID,
    day: date,
    spent_seconds: int,
    raw_balance_s: int | None = None,
    raw_limit_today_s: int | None = None,
) -> None:
    """Idempotent MAX-merge: replaying the same (or an older) absolute
    counter is always safe, out-of-order arrival is harmless. See PLAN
    "Idempotency"."""
    stmt = pg_insert(UsageCounter).values(
        user_id=user_id,
        device_id=device_id,
        day=day,
        spent_seconds=spent_seconds,
        raw_balance_s=raw_balance_s,
        raw_limit_today_s=raw_limit_today_s,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[UsageCounter.user_id, UsageCounter.device_id, UsageCounter.day],
        set_={
            "spent_seconds": text("GREATEST(usage_counters.spent_seconds, EXCLUDED.spent_seconds)"),
            "raw_balance_s": stmt.excluded.raw_balance_s,
            "raw_limit_today_s": stmt.excluded.raw_limit_today_s,
            "updated_at": text("now()"),
        },
    )
    await session.execute(stmt)


async def insert_activity_interval(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    device_id: uuid.UUID,
    day: date,
    start: datetime,
    end: datetime,
    window_end_ts: datetime,
) -> None:
    """Idempotent on (device_id, window_end_ts) -- a retried /sync POST for
    the same tick is a no-op, per PLAN's idempotency requirement."""
    stmt = pg_insert(ActivityInterval).values(
        id=uuid.uuid4(),
        user_id=user_id,
        device_id=device_id,
        day=day,
        span=text("tstzrange(:start, :end)").bindparams(start=start, end=end),
        window_end_ts=window_end_ts,
    )
    stmt = stmt.on_conflict_do_nothing(
        index_elements=[ActivityInterval.device_id, ActivityInterval.window_end_ts]
    )
    await session.execute(stmt)


async def global_spent_wallclock(session: AsyncSession, *, user_id: uuid.UUID, day: date) -> int:
    """Wall-clock union of all devices' activity for (user, day) -- the
    'burn once' accounting mode. Validated against real Postgres 16 with an
    overlapping two-device scenario (30min + 30min overlapping by 15min ->
    45min, not 60min) during Phase 1 implementation."""
    result = await session.execute(
        text(
            """
            SELECT COALESCE(
                (SELECT EXTRACT(EPOCH FROM SUM(upper(r) - lower(r)))::bigint
                 FROM unnest(
                     (SELECT range_agg(span) FROM activity_intervals
                      WHERE user_id = :user_id AND day = :day)
                 ) AS r),
                0
            ) AS global_spent_s
            """
        ),
        {"user_id": user_id, "day": day},
    )
    return int(result.scalar_one())


async def global_spent_parallel(session: AsyncSession, *, user_id: uuid.UUID, day: date) -> int:
    """Sum of per-device counters -- the 'parallel' accounting mode (each
    device's time counts independently, concurrent sessions stack)."""
    result = await session.execute(
        text(
            """
            SELECT COALESCE(SUM(spent_seconds), 0) AS global_spent_s
            FROM usage_counters WHERE user_id = :user_id AND day = :day
            """
        ),
        {"user_id": user_id, "day": day},
    )
    return int(result.scalar_one())


async def device_spent_today(
    session: AsyncSession, *, user_id: uuid.UUID, device_id: uuid.UUID, day: date
) -> int:
    """This device's own MAX-merged counter -- used to compute remote_spent_s
    = global_spent_s - device's own contribution."""
    result = await session.execute(
        text(
            """
            SELECT COALESCE(spent_seconds, 0) FROM usage_counters
            WHERE user_id = :user_id AND device_id = :device_id AND day = :day
            """
        ),
        {"user_id": user_id, "device_id": device_id, "day": day},
    )
    row = result.first()
    return int(row[0]) if row else 0
