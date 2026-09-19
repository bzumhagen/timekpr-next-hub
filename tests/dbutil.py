"""Shared helper for tests marked `db` (needs a reachable Postgres).

The DB-backed integration tests `TRUNCATE` the database they run against,
and `DATABASE_URL` defaults to the same database the dev hub itself uses -- a working
`make dev` session sitting next to a stray `pytest` run could lose data with
no warning. `require_db()` is the single choke point all DB-backed tests
route through: it insists the target database name ends in `_test`, and
skips (rather than silently running) when Postgres isn't reachable, unless
`TIMEKPR_HUB_REQUIRE_DB=1` asks for a hard failure instead (used by
`make test-db` / `make test-all`, so CI doesn't silently report a green run
that skipped every DB test).
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.sql import text

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://timekpr_hub:timekpr_hub@127.0.0.1:55432/timekpr_hub_test",
)

_REQUIRE_DB = os.environ.get("TIMEKPR_HUB_REQUIRE_DB") == "1"


def _assert_test_database(url: str) -> None:
    db_name = make_url(url).database or ""
    if not db_name.endswith("_test"):
        raise RuntimeError(
            f"refusing to run DB-backed tests against database {db_name!r} "
            f"(TEST_DATABASE_URL={url!r}) -- these tests TRUNCATE their "
            "tables, and the database name must end in '_test' as a guard "
            "against accidentally pointing this at a dev/prod database. "
            "See `make db-up` / deploy/dev-initdb.sql for the intended "
            "timekpr_hub_test database."
        )


async def _db_reachable(url: str) -> bool:
    try:
        engine = create_async_engine(url)
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        await engine.dispose()
        return True
    except Exception:
        return False


async def require_db(url: str = TEST_DATABASE_URL) -> None:
    """Call at the top of a `db`-marked fixture. Raises if `url` doesn't look
    like a test database; skips (or fails, under TIMEKPR_HUB_REQUIRE_DB=1) if
    it isn't reachable."""
    _assert_test_database(url)
    if await _db_reachable(url):
        return
    message = f"no Postgres reachable at {url}"
    if _REQUIRE_DB:
        pytest.fail(message)
    pytest.skip(message)
