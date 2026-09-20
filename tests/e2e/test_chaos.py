"""Exercise the agent's failure-mode handling against the same real
hub+Postgres harness as
tests/e2e/test_acceptance.py, rather than only the scripted-hub unit tests
in tests/unit/test_agent_run_tick.py. Corrupted-state.json coverage already
lives in tests/unit/test_agent_state.py (it needs no DB, so it stays there
rather than moving into a `db`-marked module); the two scenarios below are
specifically ones that need a real hub round trip to mean anything.
"""

from __future__ import annotations

import uuid

import pytest
from timekpr_hub_agent.tick import run_tick

from tests.e2e.harness import (
    FakeEnforcer,
    FlakyHubClient,
    SimulatedDevice,
    VirtualClock,
    enroll_device,
    tick_device,
)
from tests.fakes.fake_timekpr import FakeTimekprDaemon

# `live_hub` (used as a parameter below) is a fixture from tests/e2e/conftest.py --
# pytest injects it by name, no import needed or wanted here (see that file).

pytestmark = pytest.mark.db

USERNAME = "kiddo"
ONE_HOUR = 3600
INTERVAL_S = 10


def _enroll_one(base_url: str, tmp_path):
    return enroll_device(
        base_url=base_url,
        token_path=tmp_path / "token",
        machine_id=f"machine-{uuid.uuid4()}",
        hostname="device",
        local_users=[USERNAME],
    )


def test_hub_outage_buffers_spans_and_replays_them_on_recovery(live_hub, tmp_path):
    """A multi-tick hub outage must not lose activity from the wall-clock
    union: each failed sync buffers its span (UserState.pending_spans,
    tick.py's _buffer_unsent_span), and the first successful sync afterward
    replays all of them alongside its own (insert_activity_interval is
    idempotent, so a retried delivery is always safe).

    The outage starts only after several real baseline ticks -- more
    elapsed time than FakeTimekprDaemon's `save_interval_s` (30s, matching
    TK_SAVE_INTERVAL), not just one. Without that head start, the first
    tick's authoritative '=' write and a second '=' write from
    `_apply_offline_policy`'s "capped" branch could each discard
    not-yet-flushed `spent_day_s` before the first flush, landing it back
    on its pre-discard value and silently swallowing a span via the
    documented "'=' regression trap" (convergence.py) -- an artifact of
    compressed-time writes landing closer together than a real device's
    ever would, unrelated to the outage this test is actually about."""
    clock = VirtualClock.starting_at()
    daemon = FakeTimekprDaemon(limit_today_s=ONE_HOUR)
    real_hub = _enroll_one(live_hub, tmp_path)
    device = SimulatedDevice(hub=real_hub, enforcer=FakeEnforcer({USERNAME: daemon}, clock=clock))

    for _ in range(4):
        tick_device(
            device, clock, managed_users=[USERNAME], active_users=frozenset({USERNAME}), interval_s=INTERVAL_S
        )
    assert device.state.users[USERNAME].pending_spans == []
    spent_before_outage = daemon.spent_day_s

    device.hub = FlakyHubClient(real_hub, fail_times=2)
    for _ in range(2):
        tick_device(
            device, clock, managed_users=[USERNAME], active_users=frozenset({USERNAME}), interval_s=INTERVAL_S
        )
    assert len(device.state.users[USERNAME].pending_spans) == 2

    # Recovery: this tick reaches the real hub, replaying both buffered
    # spans plus its own.
    device.hub = real_hub
    tick_device(
        device, clock, managed_users=[USERNAME], active_users=frozenset({USERNAME}), interval_s=INTERVAL_S
    )
    assert device.state.users[USERNAME].pending_spans == []

    # Nothing was lost from the daemon's own local accounting either way --
    # the point of buffering is that the hub's wall-clock union doesn't lose
    # this activity, not that any local time was refunded.
    assert daemon.spent_day_s == spent_before_outage + 3 * INTERVAL_S


def test_clock_jump_backwards_does_not_break_the_sync(live_hub, tmp_path):
    """A clock correction (NTP step, manual date change) that moves `now`
    backwards across a tick must not crash the agent or corrupt the hub's
    union: the span this tick would naively construct starts and ends
    before the previous tick's own emitted end, which tick.py's
    last_tick_utc clamp and sync.py's end<=start guard exist specifically
    to keep out of activity_intervals."""
    clock = VirtualClock.starting_at()
    daemon = FakeTimekprDaemon(limit_today_s=ONE_HOUR)
    real_hub = _enroll_one(live_hub, tmp_path)
    device = SimulatedDevice(hub=real_hub, enforcer=FakeEnforcer({USERNAME: daemon}, clock=clock))

    tick_device(
        device, clock, managed_users=[USERNAME], active_users=frozenset({USERNAME}), interval_s=INTERVAL_S
    )

    # Jump the clock backwards by a minute, then tick again.
    clock.rewind(60)
    daemon.tick(INTERVAL_S, active=True)

    # Must complete without raising -- the assertion is that this doesn't
    # 500 or throw, not any particular resulting balance.
    run_tick(
        enforcer=device.enforcer,
        hub=device.hub,
        state=device.state,
        managed_users=[USERNAME],
        agent_version="0.1.0-e2e",
        tz_name="UTC",
        debug_clock=True,
        now=clock.now,
    )
