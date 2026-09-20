"""Reusable test harness for tests/e2e: a real agent tick loop against a
real HTTP hub (uvicorn, real Postgres), with FakeTimekprDaemon standing in
for the local timekpr daemon.

The other test layers each cover one half of this:
`test_multi_device_simulation.py` drives FakeTimekprDaemon against an
in-process `SimHub` stand-in, and `test_hub_api.py` drives the real hub with
hand-written JSON -- but neither wires the real agent tick loop to a real
hub. Both of the real bugs found by running the agent against a live daemon
were invisible to every synthetic test that existed at the time, precisely
because of that gap.

The hub runs as a genuine `uvicorn` server on an ephemeral loopback port, in
a background thread with its own event loop, rather than `httpx.ASGITransport`
(the pattern in `tests/integration/test_hub_api.py`). Two things fall out of
that choice:

  1. the app's own module-level engine (`hub/timekpr_hub/db/session.py`) is
     exercised for real, instead of overriding `get_session` per test, and
  2. the agent's real, urllib-based `HubClient` gets its first genuine
     HTTP round trip in this test suite -- everywhere else it's either
     scripted (`tests/unit/test_agent_run_tick.py`) or bypassed entirely.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TypeVar

import uvicorn
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from timekpr_hub.api.admin_auth import get_current_admin_api, get_current_admin_ui
from timekpr_hub.app import app
from timekpr_hub.db.models import Admin, User
from timekpr_hub.db.session import engine as hub_engine
from timekpr_hub_agent import state as state_mod
from timekpr_hub_agent.enforcer import UserObservation
from timekpr_hub_agent.hubclient import HubClient, HubClientConfig, HubUnreachableError
from timekpr_hub_agent.tick import run_tick

from tests.dbutil import TEST_DATABASE_URL, all_table_names, require_db
from tests.fakes.fake_timekpr import FakeTimekprDaemon

_T = TypeVar("_T")

# A stand-in admin, exactly like test_hub_api.py's _FAKE_ADMIN -- installed
# via app.dependency_overrides only so the harness can mint enrollment codes
# without running the full login flow. This works against a real uvicorn
# server the same way it works against httpx.ASGITransport: both drive the
# same underlying `app` object, and dependency_overrides is a property of
# that object, not of any particular transport.
_FAKE_ADMIN = Admin(id=uuid.uuid4(), email="e2e-admin@example.com", password_hash="unused-in-tests")


async def _override_get_current_admin() -> Admin:
    return _FAKE_ADMIN


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_until_up(base_url: str, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/healthz", timeout=1.0) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, OSError) as exc:
            last_exc = exc
        time.sleep(0.05)
    raise RuntimeError(f"e2e hub server never came up at {base_url}") from last_exc


async def start_live_hub() -> AsyncIterator[str]:
    """A real uvicorn server serving the real `timekpr_hub.app`, on an
    ephemeral port, in a background thread with its own asyncio event loop.
    An async generator rather than a fixture directly (the `live_hub`
    fixture itself lives in tests/e2e/conftest.py, so pytest can inject it
    into any test by parameter name with no explicit import -- importing a
    fixture function into a module that also uses it as a parameter name is
    a real, flagged redefinition, not just a style nit: ruff's F811 caught
    exactly that when this was first written directly in this module).

    `hub_engine.dispose()` before AND after: SQLAlchemy's async engine binds
    its asyncpg connection pool lazily, to whichever event loop first uses
    it. Some other test in this session may have already done that under
    pytest-asyncio's own (session-scoped) loop -- e.g. `test_hub_api.py`'s
    `LifespanManager(app)` runs `app.py`'s `_lifespan`, which queries via
    this same module-level engine directly, not via the overridden
    `get_session`. Disposing first forces a fresh pool, bound afresh to
    whichever loop asks next -- this fixture's own uvicorn thread. Disposing
    again on teardown leaves the engine equally reset for whatever runs
    after it, regardless of test collection order.
    """
    await require_db()

    # Truncate via a throwaway engine/loop, independent of the app's own --
    # avoids touching the app engine's loop-binding before it's deliberately
    # reset below.
    await _run_scratch(lambda session: session.execute(text(f"TRUNCATE {all_table_names()} CASCADE")))

    await hub_engine.dispose()

    app.dependency_overrides[get_current_admin_api] = _override_get_current_admin
    app.dependency_overrides[get_current_admin_ui] = _override_get_current_admin

    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on")
    server = uvicorn.Server(config)

    async def _serve_and_cleanup() -> None:
        await server.serve()
        # Dispose here, inside the same event loop the app engine's asyncpg
        # connections were actually opened under (uvicorn's own loop, not
        # this fixture's pytest-asyncio one) -- asyncpg connections are tied
        # to the loop that created them, and closing them from a *different*
        # loop after this one has already shut down raises "unable to
        # perform operation on <TCPTransport closed=True ...>". Disposing
        # here, before this loop closes, avoids that entirely.
        await hub_engine.dispose()

    thread = threading.Thread(target=lambda: asyncio.run(_serve_and_cleanup()), daemon=True)
    thread.start()
    try:
        _wait_until_up(base_url)
        yield base_url
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        app.dependency_overrides.pop(get_current_admin_api, None)
        app.dependency_overrides.pop(get_current_admin_ui, None)


async def _run_scratch(fn: Callable[[AsyncSession], Awaitable[_T]]) -> _T:
    """Run `fn(session)` against a throwaway session on its own engine bound
    to TEST_DATABASE_URL, disposed immediately after -- used for direct DB
    setup/assertions (truncation, flipping accounting_mode, reading the
    hub's own aggregate queries) without touching the live app's own engine
    or event-loop binding."""
    scratch_engine = create_async_engine(TEST_DATABASE_URL)
    try:
        session_factory = async_sessionmaker(scratch_engine, expire_on_commit=False, class_=AsyncSession)
        async with session_factory() as session:
            result = await fn(session)
            await session.commit()
            return result
    finally:
        await scratch_engine.dispose()


def mint_enrollment_code(base_url: str) -> str:
    """POST /api/v1/enrollment-codes as the fixture's overridden admin."""
    req = urllib.request.Request(
        f"{base_url}/api/v1/enrollment-codes",
        data=b"{}",
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())["code"]


def admin_api_request(base_url: str, method: str, path: str, body: dict | None = None) -> dict:
    """An admin-API call (PUT/DELETE/POST) as the fixture's overridden
    admin, for endpoints `enroll_device`/`mint_enrollment_code` don't
    already cover -- e.g. the one-day allowed-hours override."""
    data = json.dumps(body).encode() if body is not None else b"{}"
    req = urllib.request.Request(
        f"{base_url}/api/v1{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def enroll_device(
    *,
    base_url: str,
    token_path: Path,
    machine_id: str,
    hostname: str,
    local_users: list[str],
) -> HubClient:
    """Enroll a device using the real `HubClient.enroll` over real HTTP --
    the first genuine HTTP exercise this method gets anywhere in the suite
    (elsewhere it's either untested directly or the hub is driven with
    hand-written JSON)."""
    hub = HubClient(HubClientConfig(base_url=base_url, token_path=token_path))
    hub.enroll(
        enrollment_code=mint_enrollment_code(base_url),
        hostname=hostname,
        machine_id=machine_id,
        agent_version="0.1.0-e2e",
        local_users=local_users,
    )
    return hub


def set_accounting_mode(username: str, mode: str) -> None:
    """Flip a user's accounting_mode directly in the DB -- there's no
    admin-API endpoint for this, and the wall-clock
    acceptance scenario needs to exercise both modes against the same real
    union query."""

    async def _update(session: AsyncSession) -> None:
        await session.execute(
            text("UPDATE users SET accounting_mode = :mode WHERE canonical_username = :u"),
            {"mode": mode, "u": username},
        )

    asyncio.run(_run_scratch(_update))


def query_global_spent(username: str, day: date, *, mode: str = "wallclock") -> int:
    """Sync wrapper around the hub's own real aggregate query
    (`hub/timekpr_hub/services/aggregate.py`) -- lets a test assert on the
    hub's authoritative total directly, rather than trusting the agent's own
    (necessarily partial) view of it."""

    async def _query(session: AsyncSession) -> int:
        from timekpr_hub.services.aggregate import global_spent_parallel, global_spent_wallclock

        result = await session.execute(select(User.id).where(User.canonical_username == username))
        user_id = result.scalar_one()
        if mode == "parallel":
            return await global_spent_parallel(session, user_id=user_id, day=day)
        return await global_spent_wallclock(session, user_id=user_id, day=day)

    return asyncio.run(_run_scratch(_query))


class FlakyHubClient:
    """Wraps a real `HubClient`, raising `HubUnreachableError` for the first
    `fail_times` calls to `sync()`, then delegating normally -- simulates a
    hub outage a real network could produce (a 500, a timeout, a partition)
    with exact control over when it clears, for the chaos tests."""

    def __init__(self, real: HubClient, fail_times: int):
        self._real = real
        self._fail_times = fail_times
        self._calls = 0

    def sync(self, payload: dict) -> dict:
        self._calls += 1
        if self._calls <= self._fail_times:
            raise HubUnreachableError("simulated outage")
        return self._real.sync(payload)

    def enroll(self, **kwargs):
        return self._real.enroll(**kwargs)


@dataclass
class VirtualClock:
    """A monotonically-advanced, shared `datetime` -- lets an e2e scenario
    simulate tens of minutes of activity across several devices in
    milliseconds of real wall-clock time, and keeps two devices' activity
    genuinely overlapping in wall-clock terms when driven off the same
    instance (see `tick_devices_together`)."""

    _now: datetime

    @classmethod
    def starting_at(cls, dt: datetime | None = None) -> VirtualClock:
        # A Monday, so a policy's daily_limits_s[0] lands on "today" without
        # any test needing to know or care what day it actually is.
        return cls(_now=dt or datetime(2030, 1, 7, 8, 0, 0, tzinfo=UTC))

    @property
    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> datetime:
        self._now += timedelta(seconds=seconds)
        return self._now

    def rewind(self, seconds: float) -> datetime:
        self._now -= timedelta(seconds=seconds)
        return self._now


class FakeEnforcer:
    """Wraps one FakeTimekprDaemon per username -- just enough of
    TimekprEnforcer's interface for run_tick, extended (beyond the original
    private copy in tests/unit/test_agent_run_tick.py) with the four
    policy-push methods `policy_push.py::_apply_policy_push` actually calls.

    This matters for e2e: on tick 1 the agent reports
    `policy_version_applied=0` while the hub's freshly-seeded policy is
    version 1, so `sync.py` DOES send a payload and `_apply_policy_push`
    DOES run -- a no-op stub here would silently leave `daemon.limit_today_s`
    unset by the push and invalidate every downstream convergence assertion
    (`plan()` converges to `G + (L_dev - L_eff)`, which depends on
    `Observation.limit_today_s` reflecting whatever was actually pushed).

    `clock` is only consulted once a push has actually happened (i.e.
    `_daily_limits` has an entry for that user) -- until then this behaves
    exactly like the original scripted-hub tests' copy, so
    tests/unit/test_agent_run_tick.py can import this shared class in place
    of its own without changing behavior.
    """

    def __init__(self, daemons: dict[str, FakeTimekprDaemon], clock: VirtualClock | None = None):
        self.daemons = daemons
        self.clock = clock
        self._daily_limits: dict[str, list[int]] = {}
        self._weekly_limits: dict[str, int] = {}
        self._monthly_limits: dict[str, int] = {}
        self._allowed_weekdays: dict[str, list[str]] = {}
        self._allowed_hours: dict[str, dict[str, dict]] = {}
        self._track_inactive: dict[str, bool] = {}
        self._hide_tray_icon: dict[str, bool] = {}
        self._lockout: dict[str, tuple[str, str, str]] = {}
        self._playtime_enabled: dict[str, bool] = {}
        self._playtime_override: dict[str, bool] = {}
        self._playtime_unaccounted_intervals: dict[str, bool] = {}
        self._playtime_allowed_weekdays: dict[str, list[str]] = {}
        self._playtime_daily_limits: dict[str, list[int]] = {}
        self._playtime_activities: dict[str, list[tuple[str, str]]] = {}
        self.set_allowed_hours_calls = 0
        """Counts every `set_allowed_hours` invocation (successful or not)
        -- lets an e2e test assert a one-day allowed-hours override's push
        actually STOPS once the revision settles, rather than only checking
        that a push happened at all. A gate that never converges and
        re-pushes every tick forever is the failure this counter exists to
        catch (see test_acceptance.py's day-hours override test)."""

    def get_user_observation(self, username: str) -> UserObservation | None:
        d = self.daemons.get(username)
        if d is None:
            return None
        limits = self._daily_limits.get(username)
        allowed_weekdays = self._allowed_weekdays.get(username)
        if limits is not None and allowed_weekdays:
            # Mirrors real timekpr's positional (not day-keyed) indexing:
            # server/user/userdata.py:265-270 looks up today's ISO weekday
            # *within* ALLOWED_WEEKDAYS and uses that same position into
            # LIMITS_PER_WEEKDAYS -- a day not present in allowed_weekdays
            # gets limit 0, exactly like the real daemon.
            now = self.clock.now if self.clock is not None else datetime.now(UTC)
            today = str(now.isoweekday())
            idx = allowed_weekdays.index(today) if today in allowed_weekdays else -1
            d.limit_today_s = limits[idx] if 0 <= idx < len(limits) else 0
        return UserObservation(
            balance_s=d.balance_s,
            spent_day_s=d.spent_day_s,
            limit_today_s=d.limit_today_s,
            logged_in=True,
            active=True,
        )

    def set_time_left(self, username: str, op: str, seconds: int) -> bool:
        self.daemons[username].set_time_left(op, seconds)
        return True

    def set_time_limit_for_days(self, username: str, daily_limits_s: list[int]) -> bool:
        # `daily_limits_s` here is already projected to the allowed-weekdays
        # subset (agent/timekpr_hub_agent/policy_push.py::
        # _project_daily_limits_to_allowed_days), matching the real DBUS
        # call's positional semantics -- see get_user_observation above.
        self._daily_limits[username] = list(daily_limits_s)
        return True

    def set_time_limit_for_week(self, username: str, limit_s: int) -> bool:
        self._weekly_limits[username] = limit_s
        return True

    def set_time_limit_for_month(self, username: str, limit_s: int) -> bool:
        self._monthly_limits[username] = limit_s
        return True

    def set_allowed_days(self, username: str, weekdays: list[str]) -> bool:
        self._allowed_weekdays[username] = list(weekdays)
        return True

    def set_allowed_hours(self, username: str, day_number: str, hours: dict) -> bool:
        self.set_allowed_hours_calls += 1
        if not hours:
            return False
        # Real timekpr's checkAndSetAllowedHours does
        # `for rHour in list(map(str, pHourList)): ...; pHourList[rHour][...]`
        # -- it re-indexes the dict with a *stringified* key it derives by
        # iterating it, so an int-keyed dict raises KeyError there, which
        # its caller swallows into a bare DBUS failure with no visible
        # exception (server/config/configprocessor.py::
        # checkAndSetAllowedHours). Enforcing that same requirement here is
        # what makes this fake actually catch the "silently never applies"
        # class of bug instead of accepting whatever shape a caller hands
        # it -- this exact mismatch shipped once already (an int-keyed
        # `hours_to_dbus_payload`) and passed every test until it was
        # caught live on a real device, precisely because this fake didn't
        # replicate the real validation.
        if not all(isinstance(hour_key, str) for hour_key in hours):
            return False
        self._allowed_hours.setdefault(username, {})[day_number] = hours
        return True

    def set_track_inactive(self, username: str, track_inactive: bool) -> bool:
        self._track_inactive[username] = track_inactive
        return True

    def set_hide_tray_icon(self, username: str, hide: bool) -> bool:
        self._hide_tray_icon[username] = hide
        return True

    def set_lockout_type(self, username: str, lockout_type: str, wake_from: str, wake_to: str) -> bool:
        self._lockout[username] = (lockout_type, wake_from, wake_to)
        return True

    def set_playtime_enabled(self, username: str, enabled: bool) -> bool:
        self._playtime_enabled[username] = enabled
        return True

    def set_playtime_limit_override(self, username: str, override: bool) -> bool:
        self._playtime_override[username] = override
        return True

    def set_playtime_unaccounted_intervals_enabled(self, username: str, enabled: bool) -> bool:
        self._playtime_unaccounted_intervals[username] = enabled
        return True

    def set_playtime_allowed_days(self, username: str, weekdays: list[str]) -> bool:
        self._playtime_allowed_weekdays[username] = list(weekdays)
        return True

    def set_playtime_limits_for_days(self, username: str, daily_limits_s: list[int]) -> bool:
        self._playtime_daily_limits[username] = list(daily_limits_s)
        return True

    def set_playtime_activities(self, username: str, activities: list[tuple[str, str]]) -> bool:
        self._playtime_activities[username] = list(activities)
        return True


@dataclass
class SimulatedDevice:
    """One enrolled device's full agent-side state for the harness: a
    HubClient (real HTTP, or a FlakyHubClient wrapping one), a FakeEnforcer
    (one FakeTimekprDaemon per managed user), and its own persisted
    AgentState -- exactly what a real machine running
    `timekpr-hub-agent run` would have."""

    hub: HubClient | FlakyHubClient
    enforcer: FakeEnforcer
    state: state_mod.AgentState = field(default_factory=state_mod.AgentState)


def tick_devices_together(
    devices: list[tuple[SimulatedDevice, frozenset[str]]],
    clock: VirtualClock,
    *,
    managed_users: list[str],
    interval_s: int = 10,
) -> None:
    """Advance the shared clock ONCE, credit that same wall-clock window to
    every device's daemons per its own active-users set, then run each
    device's own real tick against the live hub. This is what produces
    genuinely overlapping `activity_intervals` for the wall-clock "burn
    once" scenario -- calling `tick_device` back-to-back for two devices
    would stagger their windows apart instead."""
    clock.advance(interval_s)
    for device, active_users in devices:
        for username in managed_users:
            daemon = device.enforcer.daemons.get(username)
            if daemon is not None:
                daemon.tick(interval_s, active=(username in active_users))
    for device, _active_users in devices:
        run_tick(
            enforcer=device.enforcer,
            hub=device.hub,
            state=device.state,
            managed_users=managed_users,
            agent_version="0.1.0-e2e",
            tz_name="UTC",
            debug_clock=True,
            now=clock.now,
        )


def tick_device(
    device: SimulatedDevice,
    clock: VirtualClock,
    *,
    managed_users: list[str],
    active_users: frozenset[str] = frozenset(),
    interval_s: int = 10,
) -> None:
    """Single-device convenience wrapper around `tick_devices_together`."""
    tick_devices_together([(device, active_users)], clock, managed_users=managed_users, interval_s=interval_s)
