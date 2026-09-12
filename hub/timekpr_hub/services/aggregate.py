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
    raw_balance_s: int | None = None,
    raw_limit_today_s: int | None = None,
    activity_state: str | None = None,
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
        activity_state=activity_state,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[UsageCounter.user_id, UsageCounter.device_id, UsageCounter.day],
        set_={
            "spent_seconds": text("GREATEST(usage_counters.spent_seconds, EXCLUDED.spent_seconds)"),
            "raw_balance_s": stmt.excluded.raw_balance_s,
            "raw_limit_today_s": stmt.excluded.raw_limit_today_s,
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
    45min, not 60min) during Phase 1 implementation.

    Floored at MAX(usage_counters.spent_seconds) across devices: that
    absolute, idempotently MAX-merged counter is a hard lower bound on the
    true total (a single device alone has definitely been active at least
    that long), and self-heals anything the union under-counts -- a sync
    that failed to reach the hub before the agent buffered/retried it
    (main.py's pending_spans), or a tick's span that had to be trimmed
    against the previous one (main.py's last_tick_utc clamp). The union
    stays authoritative for the *simultaneous, multi-device* "burn once"
    case, which this floor cannot express (summing per-device counters would
    double-count concurrent sessions) -- this only ever pulls the total up
    to what a single device's own honest counter already proves happened."""
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
    round-trip instead of one query per user (docs/best-practices-review.md's
    N+1 finding, used by services/summaries.py) -- same GREATEST(wall-clock
    union, MAX-merged counter) formula as the single-user version above,
    grouped by user_id. A user with neither an activity_intervals row nor a
    usage_counters row today is simply absent from the result; callers
    should default to 0. `range_agg(span)` returning a per-user multirange
    that's then unnested for its own SUM is exactly the single-user query's
    approach, just computed once per user instead of once per call; the two
    are cross-checked for equivalence in
    tests/integration/test_aggregate_postgres.py."""
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
    round-trip (docs/best-practices-review.md's N+1 finding). A user with no
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
    """`latest_activity_state` for every user in `user_ids` in one
    round-trip (docs/best-practices-review.md's N+1 finding). A user with no
    reported activity_state today is simply absent; callers should default
    to `("logged_out", None)`, same as the single-user version."""
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


async def latest_activity_state(
    session: AsyncSession, *, user_id: uuid.UUID, day: date
) -> tuple[str, datetime | None]:
    """The most recently updated device's reported activity_state for this
    user today, plus that update's timestamp (so a caller can apply its own
    staleness rule -- see hub/timekpr_hub/api/ui.py's 3x-poll-interval rule
    -- rather than trusting a state a device stopped reporting hours ago)."""
    result = await session.execute(
        text(
            """
            SELECT activity_state, updated_at FROM usage_counters
            WHERE user_id = :user_id AND day = :day AND activity_state IS NOT NULL
            ORDER BY updated_at DESC
            LIMIT 1
            """
        ),
        {"user_id": user_id, "day": day},
    )
    row = result.first()
    if row is None:
        return "logged_out", None
    return str(row[0]), row[1]


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
