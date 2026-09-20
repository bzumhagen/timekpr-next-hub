"""Hub aggregation against a real Postgres instance.

Requires a running Postgres reachable via $TEST_DATABASE_URL (default:
`postgresql+asyncpg://timekpr_hub:timekpr_hub@127.0.0.1:55432/timekpr_hub_test`,
`make db-up`) with the Alembic migrations already applied (`make
migrate-test`) -- see README.md "Testing" for the full recipe. Marked `db`
so a plain `pytest`/`make test` run skips this file entirely; `make test-db`
or `make test-all` are what actually exercise it, and fail (rather than
silently skip) if no database is reachable.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from timekpr_hub.services.aggregate import (
    devices_active_today_batch,
    global_spent_parallel,
    global_spent_parallel_batch,
    global_spent_wallclock,
    global_spent_wallclock_batch,
    insert_activity_interval,
    latest_activity_states_batch,
    upsert_usage_counter,
)
from timekpr_hub.services.limits import grants_total, grants_totals_batch

from tests.dbutil import TEST_DATABASE_URL, require_db

pytestmark = pytest.mark.db


@pytest_asyncio.fixture
async def db_session():
    await require_db()

    engine = create_async_engine(TEST_DATABASE_URL)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with session_factory() as session:
        # isolate each test: truncate the tables we touch
        await session.execute(
            text("TRUNCATE usage_counters, activity_intervals, grants, users, devices CASCADE")
        )
        await session.commit()
        yield session
        await session.execute(
            text("TRUNCATE usage_counters, activity_intervals, grants, users, devices CASCADE")
        )
        await session.commit()
    await engine.dispose()


async def _device_spent_today(
    session: AsyncSession, *, user_id: uuid.UUID, device_id: uuid.UUID, day: date
) -> int:
    """Read back one device's own MAX-merged counter -- no production
    caller reads a single device's counter in isolation (global_spent_*
    is what production code needs), so this exists only to assert on
    upsert_usage_counter's idempotent-merge behavior below."""
    result = await session.execute(
        text(
            "SELECT COALESCE(spent_seconds, 0) FROM usage_counters "
            "WHERE user_id = :u AND device_id = :d AND day = :day"
        ),
        {"u": user_id, "d": device_id, "day": day},
    )
    row = result.first()
    return int(row[0]) if row else 0


async def _make_user_and_devices(session: AsyncSession, n_devices: int = 2):
    user_id = uuid.uuid4()
    await session.execute(
        text("INSERT INTO users (id, canonical_username, display_name) VALUES (:id, :u, :d)"),
        {"id": user_id, "u": f"alpha-{user_id.hex[:8]}", "d": "Alpha"},
    )
    device_ids = []
    for i in range(n_devices):
        device_id = uuid.uuid4()
        device_ids.append(device_id)
        await session.execute(
            text("INSERT INTO devices (id, name, machine_id, token_hash) VALUES (:id, :name, :mid, :th)"),
            {
                "id": device_id,
                "name": f"dev{i}",
                # machine_id is unique-constrained across the whole devices
                # table, and several tests now call this helper more than
                # once -- keyed off device_id, not the loop index, so it
                # stays unique across calls too.
                "mid": f"m-{device_id.hex}",
                "th": f"hash{device_id.hex}",
            },
        )
    await session.commit()
    return user_id, device_ids


@pytest.mark.asyncio
async def test_usage_counter_max_merge_is_idempotent(db_session):
    user_id, (device_id,) = await _make_user_and_devices(db_session, n_devices=1)
    today = date(2026, 9, 9)

    await upsert_usage_counter(db_session, user_id=user_id, device_id=device_id, day=today, spent_seconds=100)
    await db_session.commit()
    assert await _device_spent_today(db_session, user_id=user_id, device_id=device_id, day=today) == 100

    # replay the same value -- must be a no-op
    await upsert_usage_counter(db_session, user_id=user_id, device_id=device_id, day=today, spent_seconds=100)
    await db_session.commit()
    assert await _device_spent_today(db_session, user_id=user_id, device_id=device_id, day=today) == 100

    # an older/lower value arriving out of order must NOT decrease the counter
    await upsert_usage_counter(db_session, user_id=user_id, device_id=device_id, day=today, spent_seconds=40)
    await db_session.commit()
    assert await _device_spent_today(db_session, user_id=user_id, device_id=device_id, day=today) == 100

    # a genuinely larger value does advance it
    await upsert_usage_counter(db_session, user_id=user_id, device_id=device_id, day=today, spent_seconds=250)
    await db_session.commit()
    assert await _device_spent_today(db_session, user_id=user_id, device_id=device_id, day=today) == 250


@pytest.mark.asyncio
async def test_wallclock_union_burn_once_two_overlapping_devices(db_session):
    """The end-to-end acceptance property behind the user's 'burn once'
    choice, run against real Postgres range_agg rather than the pure-Python
    reference implementation."""
    user_id, (dev_a, dev_b) = await _make_user_and_devices(db_session, n_devices=2)
    today = date(2026, 9, 9)

    await insert_activity_interval(
        db_session,
        user_id=user_id,
        device_id=dev_a,
        day=today,
        start=datetime(2026, 9, 9, 0, 0, 0, tzinfo=UTC),
        end=datetime(2026, 9, 9, 0, 30, 0, tzinfo=UTC),
        window_end_ts=datetime(2026, 9, 9, 0, 30, 0, tzinfo=UTC),
    )
    await insert_activity_interval(
        db_session,
        user_id=user_id,
        device_id=dev_b,
        day=today,
        start=datetime(2026, 9, 9, 0, 15, 0, tzinfo=UTC),
        end=datetime(2026, 9, 9, 0, 45, 0, tzinfo=UTC),
        window_end_ts=datetime(2026, 9, 9, 0, 45, 0, tzinfo=UTC),
    )
    await db_session.commit()

    # 30min + 30min overlapping by 15min -> union is 45min, not 60min
    assert await global_spent_wallclock(db_session, user_id=user_id, day=today) == 2700


@pytest.mark.asyncio
async def test_wallclock_union_replay_is_idempotent(db_session):
    """Retrying the same /sync POST (same window_end_ts) must not inflate
    the union -- this is the ON CONFLICT DO NOTHING idempotency guarantee."""
    user_id, (dev_a,) = await _make_user_and_devices(db_session, n_devices=1)
    today = date(2026, 9, 9)

    for _ in range(3):  # simulate 3 retries of the same tick
        await insert_activity_interval(
            db_session,
            user_id=user_id,
            device_id=dev_a,
            day=today,
            start=datetime(2026, 9, 9, 0, 0, 0, tzinfo=UTC),
            end=datetime(2026, 9, 9, 0, 10, 0, tzinfo=UTC),
            window_end_ts=datetime(2026, 9, 9, 0, 10, 0, tzinfo=UTC),
        )
    await db_session.commit()

    assert await global_spent_wallclock(db_session, user_id=user_id, day=today) == 600


@pytest.mark.asyncio
async def test_wallclock_floor_self_heals_a_union_that_lost_spans_to_an_outage(db_session):
    """global_spent_wallclock must never read BELOW a single device's own
    absolute counter -- that counter is idempotently MAX-merged and can
    only be an honest floor. This is what makes the hub UI self-heal
    minutes an outage would otherwise permanently erase from the union
    (docs/agent-live-test-findings.md-style live discrepancy): the device's
    cumulative_spent_s reflects everything it ever burned, even ticks whose
    active_spans never made it to the hub before a later successful sync
    replays them (main.py's pending_spans) -- and even before that replay
    lands, this floor already reports the true total."""
    user_id, (dev_a,) = await _make_user_and_devices(db_session, n_devices=1)
    today = date(2026, 9, 9)

    # Only a 5-minute span made it into activity_intervals (as if an outage
    # swallowed the rest), but the absolute counter -- reported in the same
    # /sync call -- already reflects 40 real minutes spent.
    await insert_activity_interval(
        db_session,
        user_id=user_id,
        device_id=dev_a,
        day=today,
        start=datetime(2026, 9, 9, 0, 0, 0, tzinfo=UTC),
        end=datetime(2026, 9, 9, 0, 5, 0, tzinfo=UTC),
        window_end_ts=datetime(2026, 9, 9, 0, 5, 0, tzinfo=UTC),
    )
    await upsert_usage_counter(db_session, user_id=user_id, device_id=dev_a, day=today, spent_seconds=2400)
    await db_session.commit()

    assert await global_spent_wallclock(db_session, user_id=user_id, day=today) == 2400


@pytest.mark.asyncio
async def test_wallclock_floor_does_not_override_a_larger_simultaneous_union(db_session):
    """The floor must never pull the total DOWN either -- when the union
    (two devices, non-overlapping) is already larger than any single
    device's own counter, the union stays authoritative."""
    user_id, (dev_a, dev_b) = await _make_user_and_devices(db_session, n_devices=2)
    today = date(2026, 9, 9)

    await insert_activity_interval(
        db_session,
        user_id=user_id,
        device_id=dev_a,
        day=today,
        start=datetime(2026, 9, 9, 0, 0, 0, tzinfo=UTC),
        end=datetime(2026, 9, 9, 0, 30, 0, tzinfo=UTC),
        window_end_ts=datetime(2026, 9, 9, 0, 30, 0, tzinfo=UTC),
    )
    await insert_activity_interval(
        db_session,
        user_id=user_id,
        device_id=dev_b,
        day=today,
        start=datetime(2026, 9, 9, 1, 0, 0, tzinfo=UTC),
        end=datetime(2026, 9, 9, 1, 30, 0, tzinfo=UTC),
        window_end_ts=datetime(2026, 9, 9, 1, 30, 0, tzinfo=UTC),
    )
    await upsert_usage_counter(db_session, user_id=user_id, device_id=dev_a, day=today, spent_seconds=1800)
    await upsert_usage_counter(db_session, user_id=user_id, device_id=dev_b, day=today, spent_seconds=1800)
    await db_session.commit()

    # Union: 30min + 30min, non-overlapping -> 3600. Each device's own
    # counter is only 1800 -- the floor must not clamp the total down to that.
    assert await global_spent_wallclock(db_session, user_id=user_id, day=today) == 3600


@pytest.mark.asyncio
async def test_global_spent_wallclock_batch_matches_single_user_calls(db_session):
    """`global_spent_wallclock_batch` (the batched form used by
    services/summaries.py) must return exactly what calling the
    already-verified single-user `global_spent_wallclock` once per user
    would -- including for a third user with no data at all, who must
    simply be absent rather than reported as 0 or raising."""
    user_a, (dev_a1, dev_a2) = await _make_user_and_devices(db_session, n_devices=2)
    user_b, (dev_b1,) = await _make_user_and_devices(db_session, n_devices=1)
    user_c, _ = await _make_user_and_devices(db_session, n_devices=1)  # no activity at all
    today = date(2026, 9, 9)

    # user_a: two overlapping devices, 30min + 30min overlapping 15min -> 45min
    await insert_activity_interval(
        db_session,
        user_id=user_a,
        device_id=dev_a1,
        day=today,
        start=datetime(2026, 9, 9, 0, 0, 0, tzinfo=UTC),
        end=datetime(2026, 9, 9, 0, 30, 0, tzinfo=UTC),
        window_end_ts=datetime(2026, 9, 9, 0, 30, 0, tzinfo=UTC),
    )
    await insert_activity_interval(
        db_session,
        user_id=user_a,
        device_id=dev_a2,
        day=today,
        start=datetime(2026, 9, 9, 0, 15, 0, tzinfo=UTC),
        end=datetime(2026, 9, 9, 0, 45, 0, tzinfo=UTC),
        window_end_ts=datetime(2026, 9, 9, 0, 45, 0, tzinfo=UTC),
    )
    # user_b: one device, floor case -- a short span but a larger counter
    await insert_activity_interval(
        db_session,
        user_id=user_b,
        device_id=dev_b1,
        day=today,
        start=datetime(2026, 9, 9, 0, 0, 0, tzinfo=UTC),
        end=datetime(2026, 9, 9, 0, 5, 0, tzinfo=UTC),
        window_end_ts=datetime(2026, 9, 9, 0, 5, 0, tzinfo=UTC),
    )
    await upsert_usage_counter(db_session, user_id=user_b, device_id=dev_b1, day=today, spent_seconds=2400)
    await db_session.commit()

    expected = {
        user_a: await global_spent_wallclock(db_session, user_id=user_a, day=today),
        user_b: await global_spent_wallclock(db_session, user_id=user_b, day=today),
    }
    assert expected == {user_a: 2700, user_b: 2400}

    batched = await global_spent_wallclock_batch(db_session, user_ids=[user_a, user_b, user_c], day=today)
    assert batched == expected  # user_c absent, not 0


@pytest.mark.asyncio
async def test_global_spent_parallel_batch_matches_single_user_calls(db_session):
    user_a, (dev_a1, dev_a2) = await _make_user_and_devices(db_session, n_devices=2)
    user_b, (dev_b1,) = await _make_user_and_devices(db_session, n_devices=1)
    user_c, _ = await _make_user_and_devices(db_session, n_devices=1)
    today = date(2026, 9, 9)

    await upsert_usage_counter(db_session, user_id=user_a, device_id=dev_a1, day=today, spent_seconds=600)
    await upsert_usage_counter(db_session, user_id=user_a, device_id=dev_a2, day=today, spent_seconds=900)
    await upsert_usage_counter(db_session, user_id=user_b, device_id=dev_b1, day=today, spent_seconds=1200)
    await db_session.commit()

    expected = {
        user_a: await global_spent_parallel(db_session, user_id=user_a, day=today),
        user_b: await global_spent_parallel(db_session, user_id=user_b, day=today),
    }
    assert expected == {user_a: 1500, user_b: 1200}

    batched = await global_spent_parallel_batch(db_session, user_ids=[user_a, user_b, user_c], day=today)
    assert batched == expected


@pytest.mark.asyncio
async def test_devices_active_today_batch_lists_only_devices_with_positive_spend(db_session):
    user_a, (dev_a1, dev_a2) = await _make_user_and_devices(db_session, n_devices=2)
    user_b, (dev_b1,) = await _make_user_and_devices(db_session, n_devices=1)
    user_c, _ = await _make_user_and_devices(db_session, n_devices=1)  # never synced
    today = date(2026, 9, 9)

    await upsert_usage_counter(db_session, user_id=user_a, device_id=dev_a1, day=today, spent_seconds=100)
    # dev_a2 synced but with zero spend -- must not appear.
    await upsert_usage_counter(db_session, user_id=user_a, device_id=dev_a2, day=today, spent_seconds=0)
    await upsert_usage_counter(db_session, user_id=user_b, device_id=dev_b1, day=today, spent_seconds=50)
    await db_session.commit()

    result = await devices_active_today_batch(db_session, user_ids=[user_a, user_b, user_c], day=today)
    assert result[user_a] == ["dev0"]
    assert result[user_b] == ["dev0"]
    assert user_c not in result


@pytest.mark.asyncio
async def test_latest_activity_states_batch_reports_each_users_most_recent_state(db_session):
    user_a, (dev_a,) = await _make_user_and_devices(db_session, n_devices=1)
    user_b, (dev_b,) = await _make_user_and_devices(db_session, n_devices=1)
    user_c, _ = await _make_user_and_devices(db_session, n_devices=1)  # never reported
    today = date(2026, 9, 9)

    await upsert_usage_counter(
        db_session, user_id=user_a, device_id=dev_a, day=today, spent_seconds=100, activity_state="draining"
    )
    await upsert_usage_counter(
        db_session, user_id=user_b, device_id=dev_b, day=today, spent_seconds=50, activity_state="idle"
    )
    await db_session.commit()

    batched = await latest_activity_states_batch(db_session, user_ids=[user_a, user_b, user_c], day=today)
    assert batched[user_a][0] == "draining"
    assert batched[user_b][0] == "idle"
    assert user_c not in batched  # never reported -- absent, not ("logged_out", None)


@pytest.mark.asyncio
async def test_grants_totals_batch_matches_single_user_calls(db_session):
    from timekpr_hub.db.models import Grant

    user_a, _ = await _make_user_and_devices(db_session, n_devices=1)
    user_b, _ = await _make_user_and_devices(db_session, n_devices=1)
    user_c, _ = await _make_user_and_devices(db_session, n_devices=1)  # no grants today
    today = date(2026, 9, 9)

    db_session.add_all(
        [
            Grant(id=uuid.uuid4(), user_id=user_a, day=today, seconds=600, reason="a1", source="admin"),
            Grant(id=uuid.uuid4(), user_id=user_a, day=today, seconds=300, reason="a2", source="admin"),
            Grant(id=uuid.uuid4(), user_id=user_b, day=today, seconds=-120, reason="b1", source="admin"),
        ]
    )
    await db_session.commit()

    expected = {
        user_a: await grants_total(db_session, user_id=user_a, day=today),
        user_b: await grants_total(db_session, user_id=user_b, day=today),
    }
    assert expected == {user_a: 900, user_b: -120}

    batched = await grants_totals_batch(db_session, user_ids=[user_a, user_b, user_c], day=today)
    assert batched == expected  # user_c absent, not 0
