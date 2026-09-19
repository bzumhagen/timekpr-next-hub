"""The three PLAN "Phase 1 acceptance test" scenarios (CHECKLIST.md), never
run against real components until now -- see tests/e2e/harness.py's module
docstring for why that mattered. Each scenario here drives the real agent
tick loop (`timekpr_hub_agent.main.run_tick`), the real convergence math
(`timekpr_hub_core.convergence.plan`), a real hub over real HTTP (uvicorn),
and real Postgres, with FakeTimekprDaemon standing in only for the local
timekpr daemon itself (already validated against the real one -- see its
own module docstring).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from timekpr_hub_agent.fake_timekpr import FakeTimekprDaemon

from tests.e2e.harness import (
    FakeEnforcer,
    SimulatedDevice,
    VirtualClock,
    enroll_device,
    parent_api_request,
    query_global_spent,
    set_accounting_mode,
    tick_device,
    tick_devices_together,
)

# `live_hub` (used as a parameter below) is a fixture from tests/e2e/conftest.py --
# pytest injects it by name, no import needed or wanted here (see that file).

pytestmark = pytest.mark.db

USERNAME = "kiddo"
ONE_HOUR = 3600
INTERVAL_S = 10


def _setup_two_devices(
    base_url: str, tmp_path, clock: VirtualClock
) -> tuple[SimulatedDevice, SimulatedDevice]:
    """Enroll two devices for the same brand-new hub user via the real
    /enroll endpoint -- the hub seeds a 1h/day default policy (no
    local_policies reported), matching PLAN's "user with 1h limit"."""
    daemon_a = FakeTimekprDaemon(limit_today_s=ONE_HOUR)
    daemon_b = FakeTimekprDaemon(limit_today_s=ONE_HOUR)

    hub_a = enroll_device(
        base_url=base_url,
        token_path=tmp_path / "device-a-token",
        machine_id=f"machine-a-{uuid.uuid4()}",
        hostname="device-a",
        local_users=[USERNAME],
    )
    hub_b = enroll_device(
        base_url=base_url,
        token_path=tmp_path / "device-b-token",
        machine_id=f"machine-b-{uuid.uuid4()}",
        hostname="device-b",
        local_users=[USERNAME],
    )

    device_a = SimulatedDevice(hub=hub_a, enforcer=FakeEnforcer({USERNAME: daemon_a}, clock=clock))
    device_b = SimulatedDevice(hub=hub_b, enforcer=FakeEnforcer({USERNAME: daemon_b}, clock=clock))
    return device_a, device_b


def _burn_minutes(
    device: SimulatedDevice, clock: VirtualClock, *, minutes: int, interval_s: int = INTERVAL_S
) -> None:
    for _ in range((minutes * 60) // interval_s):
        tick_device(
            device, clock, managed_users=[USERNAME], active_users=frozenset({USERNAME}), interval_s=interval_s
        )


def test_sequential_two_device_handoff(live_hub, tmp_path):
    """PLAN acceptance #1: two devices, user with 1h limit; burn 40min on A,
    log into B, confirm ~20min left within one sync interval."""
    clock = VirtualClock.starting_at()
    _device_a, device_b = _setup_two_devices(live_hub, tmp_path, clock)
    daemon_b = device_b.enforcer.daemons[USERNAME]

    _burn_minutes(_device_a, clock, minutes=40)
    # B observes the pooled total for the first time.
    tick_device(device_b, clock, managed_users=[USERNAME], active_users=frozenset(), interval_s=INTERVAL_S)

    b_time_left = daemon_b.limit_today_s - daemon_b.balance_s
    assert abs(b_time_left - 1200) <= 2 * INTERVAL_S


def test_both_devices_lock_at_the_shared_limit(live_hub, tmp_path):
    """PLAN acceptance #2: burn the remaining 20min on B, confirm both lock,
    and the pool's total consumption stays within the PLAN overshoot bound
    (D*N + TK_POLLTIME(3s) + TIMEKPR_TERMINATION_TIME(15s), D=2 devices,
    N=10s interval -- the same bound tests/integration/
    test_multi_device_simulation.py already checks in its own SimHub-based
    scenario)."""
    clock = VirtualClock.starting_at()
    device_a, device_b = _setup_two_devices(live_hub, tmp_path, clock)
    daemon_a = device_a.enforcer.daemons[USERNAME]
    daemon_b = device_b.enforcer.daemons[USERNAME]

    _burn_minutes(device_a, clock, minutes=40)

    # A little extra beyond the theoretical 20min so B is observed AFTER
    # lockout takes effect, not just at the edge.
    ticks_to_exhaust = (20 * 60) // INTERVAL_S + 5
    for _ in range(ticks_to_exhaust):
        tick_device(
            device_b,
            clock,
            managed_users=[USERNAME],
            active_users=frozenset({USERNAME}),
            interval_s=INTERVAL_S,
        )
        if daemon_b.balance_s >= daemon_b.limit_today_s:
            break

    assert daemon_b.balance_s >= daemon_b.limit_today_s

    # A's very next tick must also converge to locked, even though A itself
    # hasn't burned anything since -- it's the pooled total that locks it.
    tick_device(device_a, clock, managed_users=[USERNAME], active_users=frozenset(), interval_s=INTERVAL_S)
    assert daemon_a.balance_s >= daemon_a.limit_today_s

    # Sequential (non-overlapping) use, so each daemon's own honest counter
    # sums to the true pool total -- no wall-clock union subtlety here
    # (that's scenario 3, below).
    total_spent_s = daemon_a.spent_day_s + daemon_b.spent_day_s
    overshoot_bound = 2 * INTERVAL_S + 3 + 15
    assert total_spent_s <= ONE_HOUR + overshoot_bound


def test_wallclock_accounting_counts_overlapping_use_once(live_hub, tmp_path):
    """PLAN acceptance #3: both devices active simultaneously for 30min,
    confirm ~30min consumed pool-wide, not 60 -- this is the one scenario
    that drives real overlapping activity_intervals through the real agent
    and the real `range_agg` union (hub/timekpr_hub/services/aggregate.py),
    rather than a hand-rolled overlap fixture."""
    clock = VirtualClock.starting_at()
    device_a, device_b = _setup_two_devices(live_hub, tmp_path, clock)

    for _ in range((30 * 60) // INTERVAL_S):
        tick_devices_together(
            [(device_a, frozenset({USERNAME})), (device_b, frozenset({USERNAME}))],
            clock,
            managed_users=[USERNAME],
            interval_s=INTERVAL_S,
        )

    # /sync (hub/timekpr_hub/api/sync.py) stamps every row it writes with the
    # HUB's own `datetime.now(UTC)`, not anything from the agent's payload --
    # it never even looks at the VirtualClock's date, only at the wall-clock
    # *durations* within each reported span. So the query below must use
    # today's real date, not `clock.now.date()` (2030-01-07): querying by
    # the virtual date found nothing and silently read back 0 the first time
    # this test was run for real, which is exactly the kind of gap in
    # understanding this harness exists to surface.
    today = datetime.now(UTC).date()
    wallclock_total = query_global_spent(USERNAME, today, mode="wallclock")
    assert abs(wallclock_total - 1800) <= 2 * INTERVAL_S  # ~30min, not ~60min

    # Flipping to 'parallel' accounting for the same recorded activity
    # should read close to double -- proves the assertion above is actually
    # exercising the union, not merely tautological.
    set_accounting_mode(USERNAME, "parallel")
    parallel_total = query_global_spent(USERNAME, today, mode="parallel")
    assert parallel_total >= wallclock_total * 1.8


def test_day_hour_override_is_pushed_and_reverted(live_hub, tmp_path):
    """A real one-day allowed-hours override, driven through the real
    parent API, the real /sync push gate, and the real agent tick loop:
    PUT an override for today -> a tick applies it; DELETE it -> the next
    tick reverts weekday W back to the standing hours -- and, just as
    important, once the revision has settled, FURTHER ticks push nothing
    at all (a gate that never converges and keeps re-pushing every 20s
    forever is the most likely bug in this feature, and nothing else in
    the suite catches it -- see `FakeEnforcer.set_allowed_hours_calls`)."""
    clock = VirtualClock.starting_at()
    daemon = FakeTimekprDaemon(limit_today_s=ONE_HOUR)
    hub = enroll_device(
        base_url=live_hub,
        token_path=tmp_path / "device-token",
        machine_id=f"machine-{uuid.uuid4()}",
        hostname="device",
        local_users=[USERNAME],
    )
    enforcer = FakeEnforcer({USERNAME: daemon})
    device = SimulatedDevice(hub=hub, enforcer=enforcer)

    today = datetime.now(UTC).date()
    weekday = str(today.isoweekday())

    # Tick 1: no override yet -- the freshly-seeded default policy's hours
    # are unrestricted for every weekday.
    tick_device(device, clock, managed_users=[USERNAME])
    assert len(enforcer._allowed_hours[USERNAME][weekday]) == 24
    calls_after_tick_1 = enforcer.set_allowed_hours_calls

    # A tick with nothing changed must push nothing further.
    tick_device(device, clock, managed_users=[USERNAME])
    assert enforcer.set_allowed_hours_calls == calls_after_tick_1

    # PUT a narrower override for today ("home early" widened to "any
    # time" wouldn't be visible against an already-unrestricted default,
    # so this narrows it instead -- the opposite direction, but the same
    # push/revert mechanism).
    parent_api_request(
        live_hub,
        "PUT",
        f"/users/{USERNAME}/day-hours",
        {"day": today.isoformat(), "mode": "window", "from_min": 9 * 60, "to_min": 17 * 60, "reason": "e2e"},
    )
    tick_device(device, clock, managed_users=[USERNAME])
    assert len(enforcer._allowed_hours[USERNAME][weekday]) == 8  # 09:00-17:00
    calls_after_override = enforcer.set_allowed_hours_calls
    assert calls_after_override > calls_after_tick_1

    # Once applied, a further unchanged tick must push nothing more --
    # this is the assertion that catches a gate stuck re-pushing forever.
    tick_device(device, clock, managed_users=[USERNAME])
    assert enforcer.set_allowed_hours_calls == calls_after_override

    # DELETE the override: the next tick must revert weekday W back to
    # unrestricted.
    parent_api_request(live_hub, "DELETE", f"/users/{USERNAME}/day-hours/{today.isoformat()}")
    tick_device(device, clock, managed_users=[USERNAME])
    assert len(enforcer._allowed_hours[USERNAME][weekday]) == 24
    calls_after_revert = enforcer.set_allowed_hours_calls
    assert calls_after_revert > calls_after_override

    # And, again, the push must stop once the revert has landed.
    tick_device(device, clock, managed_users=[USERNAME])
    assert enforcer.set_allowed_hours_calls == calls_after_revert
