"""Hub-side aggregation: the MAX-merge of usage_counters and the wall-clock
union of activity_intervals.

Agents report absolute counters, never deltas, so a replayed or duplicated
report is idempotent; the wall-clock union is what makes concurrent
activity on two devices burn budget once.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import bindparam, text
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
    activity_state: str | None = None,
) -> None:
    """Idempotent MAX-merge: replaying the same (or an older) absolute
    counter is always safe, out-of-order arrival is harmless."""
    stmt = pg_insert(UsageCounter).values(
        user_id=user_id,
        device_id=device_id,
        day=day,
        spent_seconds=spent_seconds,
        activity_state=activity_state,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[UsageCounter.user_id, UsageCounter.device_id, UsageCounter.day],
        set_={
            "spent_seconds": text("GREATEST(usage_counters.spent_seconds, EXCLUDED.spent_seconds)"),
            "activity_state": stmt.excluded.activity_state,
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
    the same tick is a no-op."""
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
    'burn once' accounting mode: two devices active 30min each, overlapping
    by 15min, consume 45min of budget, not 60.

    Floored at MAX(usage_counters.spent_seconds) across devices: that
    absolute, idempotently MAX-merged counter is a hard lower bound (a
    single device alone was active at least that long), and self-heals
    anything the union under-counts -- a sync the agent had to buffer and
    retry (tick.py's pending_spans), or a span trimmed against the
    previous one (tick.py's last_tick_utc clamp). It only ever pulls the
    total up to what a single device's own counter already proves; the
    union stays authoritative for simultaneous multi-device "burn once",
    which summing per-device counters would double-count."""
    result = await session.execute(
        text(
            """
            SELECT GREATEST(
                COALESCE(
                    (SELECT EXTRACT(EPOCH FROM SUM(upper(r) - lower(r)))::bigint
                     FROM unnest(
                         (SELECT range_agg(span) FROM activity_intervals
                          WHERE user_id = :user_id AND day = :day)
                     ) AS r),
                    0
                ),
                COALESCE(
                    (SELECT MAX(spent_seconds) FROM usage_counters
                     WHERE user_id = :user_id AND day = :day),
                    0
                )
            ) AS global_spent_s
            """
        ),
        {"user_id": user_id, "day": day},
    )
    return int(result.scalar_one())


async def global_spent_wallclock_batch(
    session: AsyncSession, *, user_ids: list[uuid.UUID], day: date
) -> dict[uuid.UUID, int]:
    """`global_spent_wallclock` for every user in `user_ids` in one
    round-trip (services/summaries.py) -- same GREATEST(wall-clock union,
    MAX-merged counter) formula, grouped by user_id; cross-checked for
    equivalence with the single-user query in
    tests/integration/test_aggregate_postgres.py. A user with no rows
    today is absent from the result; callers should default to 0."""
    if not user_ids:
        return {}
    stmt = text(
        """
        WITH per_user_ranges AS (
            SELECT user_id, range_agg(span) AS merged
            FROM activity_intervals
            WHERE user_id IN :user_ids AND day = :day
            GROUP BY user_id
        ),
        unioned AS (
            SELECT user_id, SUM(EXTRACT(EPOCH FROM (upper(r) - lower(r))))::bigint AS union_s
            FROM per_user_ranges, unnest(merged) AS r
            GROUP BY user_id
        ),
        counters AS (
            SELECT user_id, MAX(spent_seconds) AS max_s
            FROM usage_counters
            WHERE user_id IN :user_ids AND day = :day
            GROUP BY user_id
        )
        SELECT COALESCE(u.user_id, c.user_id) AS user_id,
               GREATEST(COALESCE(u.union_s, 0), COALESCE(c.max_s, 0)) AS global_spent_s
        FROM unioned u
        FULL OUTER JOIN counters c ON c.user_id = u.user_id
        """
    ).bindparams(bindparam("user_ids", expanding=True))
    result = await session.execute(stmt, {"user_ids": user_ids, "day": day})
    return {row.user_id: int(row.global_spent_s) for row in result}


async def global_spent_parallel_batch(
    session: AsyncSession, *, user_ids: list[uuid.UUID], day: date
) -> dict[uuid.UUID, int]:
    """`global_spent_parallel` for every user in `user_ids` in one
    round-trip instead of one query per user. A user with no
    usage_counters row today is simply absent; callers should default to 0."""
    if not user_ids:
        return {}
    stmt = text(
        """
        SELECT user_id, SUM(spent_seconds) AS global_spent_s
        FROM usage_counters WHERE user_id IN :user_ids AND day = :day
        GROUP BY user_id
        """
    ).bindparams(bindparam("user_ids", expanding=True))
    result = await session.execute(stmt, {"user_ids": user_ids, "day": day})
    return {row.user_id: int(row.global_spent_s) for row in result}


async def latest_activity_states_batch(
    session: AsyncSession, *, user_ids: list[uuid.UUID], day: date
) -> dict[uuid.UUID, tuple[str, datetime]]:
    """The most recently updated device's reported activity_state for every
    user in `user_ids`, in one round-trip. A user with no reported
    activity_state today is simply absent; callers should default to
    `("logged_out", None)`."""
    if not user_ids:
        return {}
    stmt = text(
        """
        SELECT DISTINCT ON (user_id) user_id, activity_state, updated_at
        FROM usage_counters
        WHERE user_id IN :user_ids AND day = :day AND activity_state IS NOT NULL
        ORDER BY user_id, updated_at DESC
        """
    ).bindparams(bindparam("user_ids", expanding=True))
    result = await session.execute(stmt, {"user_ids": user_ids, "day": day})
    return {row.user_id: (str(row.activity_state), row.updated_at) for row in result}


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


async def global_spent_wallclock_history(
    session: AsyncSession, *, user_id: uuid.UUID, start_day: date, end_day: date
) -> dict[date, int]:
    """`global_spent_wallclock` for every day in `[start_day, end_day]` in one
    round-trip -- the usage-statistics view's day-by-day totals. Same
    GREATEST(wall-clock union, MAX-merged counter) formula as the single-day
    version, just grouped by day as well as unioning within each day. A day
    with no data at all is simply absent; callers should default to 0."""
    stmt = text(
        """
        WITH per_day_ranges AS (
            SELECT day, range_agg(span) AS merged
            FROM activity_intervals
            WHERE user_id = :user_id AND day BETWEEN :start_day AND :end_day
            GROUP BY day
        ),
        unioned AS (
            SELECT day, SUM(EXTRACT(EPOCH FROM (upper(r) - lower(r))))::bigint AS union_s
            FROM per_day_ranges, unnest(merged) AS r
            GROUP BY day
        ),
        counters AS (
            SELECT day, MAX(spent_seconds) AS max_s
            FROM usage_counters
            WHERE user_id = :user_id AND day BETWEEN :start_day AND :end_day
            GROUP BY day
        )
        SELECT COALESCE(u.day, c.day) AS day,
               GREATEST(COALESCE(u.union_s, 0), COALESCE(c.max_s, 0)) AS global_spent_s
        FROM unioned u
        FULL OUTER JOIN counters c ON c.day = u.day
        """
    )
    result = await session.execute(stmt, {"user_id": user_id, "start_day": start_day, "end_day": end_day})
    return {row.day: int(row.global_spent_s) for row in result}


async def global_spent_wallclock_window(
    session: AsyncSession, *, user_id: uuid.UUID, start_day: date, end_day: date
) -> int:
    """Pooled wall-clock spend across every day in [start_day, end_day] --
    the week/month pooling window used by /sync's effective_week_limit_s /
    effective_month_limit_s. Days are disjoint in wall-clock time, so simply
    summing each day's independently-computed union
    (`global_spent_wallclock_history`, already GREATEST-floored per day) is
    correct -- unlike within a single day, there's no cross-day overlap that
    needs merging."""
    per_day = await global_spent_wallclock_history(
        session, user_id=user_id, start_day=start_day, end_day=end_day
    )
    return sum(per_day.values())


async def global_spent_parallel_window(
    session: AsyncSession, *, user_id: uuid.UUID, start_day: date, end_day: date
) -> int:
    """Pooled 'parallel' accounting spend across [start_day, end_day] -- sum
    of every device's per-day counter, the range generalization of
    `global_spent_parallel`."""
    result = await session.execute(
        text(
            """
            SELECT COALESCE(SUM(spent_seconds), 0) AS global_spent_s
            FROM usage_counters
            WHERE user_id = :user_id AND day BETWEEN :start_day AND :end_day
            """
        ),
        {"user_id": user_id, "start_day": start_day, "end_day": end_day},
    )
    return int(result.scalar_one())


async def device_spent_for_day_by_device(
    session: AsyncSession, *, user_id: uuid.UUID, day: date
) -> dict[uuid.UUID, int]:
    """Each device's own MAX-merged counter for one day -- the stats view's
    per-device split for a selected day."""
    result = await session.execute(
        text(
            """
            SELECT device_id, spent_seconds FROM usage_counters
            WHERE user_id = :user_id AND day = :day
            """
        ),
        {"user_id": user_id, "day": day},
    )
    return {row.device_id: int(row.spent_seconds) for row in result}


async def devices_active_today_batch(
    session: AsyncSession, *, user_ids: list[uuid.UUID], day: date
) -> dict[uuid.UUID, list[str]]:
    """Each user's device *names* with any reported spend for `day`, in one
    round-trip -- `UserSummary.devices_active_today`. A user with no
    positive-spend device that day is simply absent from the result;
    callers should default to []."""
    if not user_ids:
        return {}
    stmt = text(
        """
        SELECT uc.user_id, d.name
        FROM usage_counters uc
        JOIN devices d ON d.id = uc.device_id
        WHERE uc.user_id IN :user_ids AND uc.day = :day AND uc.spent_seconds > 0
        ORDER BY uc.user_id, d.name
        """
    ).bindparams(bindparam("user_ids", expanding=True))
    result = await session.execute(stmt, {"user_ids": user_ids, "day": day})
    by_user: dict[uuid.UUID, list[str]] = {}
    for row in result:
        by_user.setdefault(row.user_id, []).append(row.name)
    return by_user
