"""Layer 4 verification (PLAN "Verification"): hub aggregation against a
real Postgres instance.

Requires a running Postgres reachable via $DATABASE_URL (or the default
`postgresql+asyncpg://timekpr_hub:timekpr_hub@localhost:5432/timekpr_hub`)
with the Alembic migrations already applied -- see deploy/docker-compose.yml
or docs/dev-setup.md for how to bring one up. Skipped automatically if no
database is reachable, so the rest of the suite (which needs no services)
still runs anywhere.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from timekpr_hub.db.session import DATABASE_URL
from timekpr_hub.services.aggregate import (
    device_spent_today,
    global_spent_wallclock,
    insert_activity_interval,
    upsert_usage_counter,
)


async def _db_reachable() -> bool:
    try:
        engine = create_async_engine(DATABASE_URL)
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        await engine.dispose()
        return True
    except Exception:
        return False


@pytest_asyncio.fixture
async def db_session():
    reachable = await _db_reachable()
    if not reachable:
        pytest.skip(f"no Postgres reachable at {DATABASE_URL}")

    engine = create_async_engine(DATABASE_URL)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with session_factory() as session:
        # isolate each test: truncate the tables we touch
        await session.execute(
            text("TRUNCATE usage_counters, activity_intervals, users, devices CASCADE")
        )
        await session.commit()
        yield session
        await session.execute(
            text("TRUNCATE usage_counters, activity_intervals, users, devices CASCADE")
        )
        await session.commit()
    await engine.dispose()


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
            text(
                "INSERT INTO devices (id, name, hostname, machine_id, token_hash, token_prefix) "
                "VALUES (:id, :name, :host, :mid, :th, :tp)"
            ),
            {
                "id": device_id,
                "name": f"dev{i}",
                "host": f"host{i}",
                "mid": f"m{i}",
                "th": f"hash{device_id.hex}",
                "tp": f"tkh_{i:04d}",
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
    assert await device_spent_today(db_session, user_id=user_id, device_id=device_id, day=today) == 100

    # replay the same value -- must be a no-op
    await upsert_usage_counter(db_session, user_id=user_id, device_id=device_id, day=today, spent_seconds=100)
    await db_session.commit()
    assert await device_spent_today(db_session, user_id=user_id, device_id=device_id, day=today) == 100

    # an older/lower value arriving out of order must NOT decrease the counter
    await upsert_usage_counter(db_session, user_id=user_id, device_id=device_id, day=today, spent_seconds=40)
    await db_session.commit()
    assert await device_spent_today(db_session, user_id=user_id, device_id=device_id, day=today) == 100

    # a genuinely larger value does advance it
    await upsert_usage_counter(db_session, user_id=user_id, device_id=device_id, day=today, spent_seconds=250)
    await db_session.commit()
    assert await device_spent_today(db_session, user_id=user_id, device_id=device_id, day=today) == 250


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
        start=datetime(2026, 9, 9, 0, 0, 0, tzinfo=timezone.utc),
        end=datetime(2026, 9, 9, 0, 30, 0, tzinfo=timezone.utc),
        window_end_ts=datetime(2026, 9, 9, 0, 30, 0, tzinfo=timezone.utc),
    )
    await insert_activity_interval(
        db_session,
        user_id=user_id,
        device_id=dev_b,
        day=today,
        start=datetime(2026, 9, 9, 0, 15, 0, tzinfo=timezone.utc),
        end=datetime(2026, 9, 9, 0, 45, 0, tzinfo=timezone.utc),
        window_end_ts=datetime(2026, 9, 9, 0, 45, 0, tzinfo=timezone.utc),
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
            start=datetime(2026, 9, 9, 0, 0, 0, tzinfo=timezone.utc),
            end=datetime(2026, 9, 9, 0, 10, 0, tzinfo=timezone.utc),
            window_end_ts=datetime(2026, 9, 9, 0, 10, 0, tzinfo=timezone.utc),
        )
    await db_session.commit()

    assert await global_spent_wallclock(db_session, user_id=user_id, day=today) == 600
