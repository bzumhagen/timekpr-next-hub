"""run_tick-level regression tests for Phase 5e/5f and observe-mode handling,
using FakeTimekprDaemon (already validated against the real daemon -- see
fake_timekpr.py's module docstring) behind a minimal fake enforcer, and a
scripted fake hub client instead of a real HubClient/httpx.
"""

from __future__ import annotations

from datetime import UTC, datetime

from timekpr_hub_agent import state as state_mod
from timekpr_hub_agent.enforcer import UserObservation
from timekpr_hub_agent.fake_timekpr import FakeTimekprDaemon
from timekpr_hub_agent.hubclient import HubUnreachableError
from timekpr_hub_agent.main import DEFAULT_OFFLINE_CAP_S, DEFAULT_OFFLINE_GRACE_S, run_tick


class FakeEnforcer:
    """Wraps one FakeTimekprDaemon per username -- just enough of
    TimekprEnforcer's interface for run_tick."""

    def __init__(self, daemons: dict[str, FakeTimekprDaemon]):
        self.daemons = daemons

    def get_user_observation(self, username: str) -> UserObservation | None:
        d = self.daemons.get(username)
        if d is None:
            return None
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


class ScriptedHub:
    """`responses` is a list of callables (payload) -> dict, or an
    exception instance/class to raise, consumed one per `sync()` call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.payloads = []

    def sync(self, payload: dict) -> dict:
        self.payloads.append(payload)
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, type) and issubclass(item, Exception):
            raise item("scripted failure")
        return item(payload) if callable(item) else item


def _sync_response(
    username: str, *, effective_limit_today_s: int, global_spent_s: int, enforcement: str = "enforce"
):
    return {
        "hub_time": datetime.now(UTC).isoformat(),
        "hub_tz": "UTC",
        "day": datetime.now(UTC).date().isoformat(),
        "iso_week": "2026-W01",
        "month": "2026-01",
        "next_poll_ms": 20000,
        "users": [
            {
                "username": username,
                "global_spent_s": global_spent_s,
                "remote_spent_s": 0,
                "effective_limit_today_s": effective_limit_today_s,
                "effective_week_limit_s": effective_limit_today_s * 7,
                "effective_month_limit_s": effective_limit_today_s * 30,
                "enforcement": enforcement,
                "suppressed": False,
                "policy_version": 1,
                "policy": None,
            }
        ],
    }


def test_pre_enrollment_usage_is_credited_not_forgiven():
    """Phase 5f: a device's first tick for a user must report whatever
    timekpr already shows as spent today, not reset to 0."""
    daemon = FakeTimekprDaemon(limit_today_s=7200)
    daemon.tick(500, active=True)  # already used 500s before the agent ever ran
    enforcer = FakeEnforcer({"alice": daemon})
    hub = ScriptedHub([_sync_response("alice", effective_limit_today_s=7200, global_spent_s=500)])
    state = state_mod.AgentState()

    run_tick(
        enforcer=enforcer, hub=hub, state=state, managed_users=["alice"], agent_version="0.1.0", tz_name="UTC"
    )

    assert hub.payloads[0]["users"][0]["cumulative_spent_s"] == 500
    assert state.users["alice"].cum_local_s == 500


def test_observe_enforcement_never_writes_a_local_limit():
    """An unmapped user (or a device explicitly set to observe-only) must
    not be converged toward the hub's placeholder 0/0 target."""
    daemon = FakeTimekprDaemon(limit_today_s=86400)
    daemon.tick(300, active=True)
    balance_before = daemon.balance_s
    enforcer = FakeEnforcer({"alice": daemon})
    hub = ScriptedHub(
        [_sync_response("alice", effective_limit_today_s=0, global_spent_s=0, enforcement="observe")]
    )
    state = state_mod.AgentState()

    run_tick(
        enforcer=enforcer, hub=hub, state=state, managed_users=["alice"], agent_version="0.1.0", tz_name="UTC"
    )

    assert daemon.balance_s == balance_before
    assert state.users["alice"].last_enforcement == "observe"


def test_offline_capped_policy_never_refunds_local_activity():
    """Phase 5e: the original bug converged every offline-past-grace tick
    to the stale last_global_spent_s, refunding whatever the child used
    locally since the last hub contact -- an offline device would stop
    counting time at all. The estimate must only ever grow while offline.

    Uses a small effective limit (400s) so the offline cap (1800s) doesn't
    bind and the test isolates the refund behavior specifically, not the
    cap. Manually backdates last_hub_contact_utc rather than sleeping, to
    actually exercise the post-grace branch instead of the (also correct,
    separately tested) in-grace early return.
    """
    daemon = FakeTimekprDaemon(limit_today_s=7200)
    enforcer = FakeEnforcer({"alice": daemon})
    state = state_mod.AgentState()

    # Tick 1: online, establishes last_global_spent_s=100 and the
    # cum_local_at_contact_s baseline.
    hub = ScriptedHub([_sync_response("alice", effective_limit_today_s=400, global_spent_s=100)])
    daemon.tick(100, active=True)
    run_tick(
        enforcer=enforcer, hub=hub, state=state, managed_users=["alice"], agent_version="0.1.0", tz_name="UTC"
    )
    assert state.users["alice"].last_global_spent_s == 100

    # Simulate grace having expired (well past DEFAULT_OFFLINE_GRACE_S).
    state.users["alice"].last_hub_contact_utc -= DEFAULT_OFFLINE_GRACE_S + 60

    # Tick 2: hub unreachable, 200s more local activity accrues.
    daemon.tick(200, active=True)
    hub2 = ScriptedHub([HubUnreachableError("connection refused")])
    run_tick(
        enforcer=enforcer,
        hub=hub2,
        state=state,
        managed_users=["alice"],
        agent_version="0.1.0",
        tz_name="UTC",
    )

    # Time left must reflect the full 300s used so far (100 known-good +
    # 200 accrued offline: last_effective_limit_today_s(400) - G_est(300)),
    # not regress toward the stale 100s figure (400 - 100 = 300, which
    # would refund the 200s offline and look like nothing happened).
    time_left = daemon.limit_today_s - daemon.balance_s
    assert time_left == 100


def test_offline_capped_policy_still_enforces_the_cap():
    """The cap itself (DEFAULT_OFFLINE_CAP_S) must still bind when the
    hub's real effective limit is generous -- "never refund" doesn't mean
    "never limit"."""
    daemon = FakeTimekprDaemon(limit_today_s=86400)
    enforcer = FakeEnforcer({"alice": daemon})
    state = state_mod.AgentState()

    hub = ScriptedHub([_sync_response("alice", effective_limit_today_s=86400, global_spent_s=0)])
    run_tick(
        enforcer=enforcer, hub=hub, state=state, managed_users=["alice"], agent_version="0.1.0", tz_name="UTC"
    )
    state.users["alice"].last_hub_contact_utc -= DEFAULT_OFFLINE_GRACE_S + 60

    daemon.tick(DEFAULT_OFFLINE_CAP_S + 600, active=True)  # well past the offline cap
    hub2 = ScriptedHub([HubUnreachableError("still down")])
    run_tick(
        enforcer=enforcer,
        hub=hub2,
        state=state,
        managed_users=["alice"],
        agent_version="0.1.0",
        tz_name="UTC",
    )

    time_left = daemon.limit_today_s - daemon.balance_s
    assert time_left == 0  # locked out at the cap, not left running on the 86400s device limit


def test_offline_within_grace_makes_no_new_write():
    """Still within the grace window: keep enforcing the frozen last-known
    target, no new DBUS write (and thus no risk of a spurious correction
    from a momentary blip)."""
    daemon = FakeTimekprDaemon(limit_today_s=7200)
    enforcer = FakeEnforcer({"alice": daemon})
    state = state_mod.AgentState()

    hub = ScriptedHub([_sync_response("alice", effective_limit_today_s=7200, global_spent_s=0)])
    run_tick(
        enforcer=enforcer, hub=hub, state=state, managed_users=["alice"], agent_version="0.1.0", tz_name="UTC"
    )
    balance_after_first_tick = daemon.balance_s

    hub2 = ScriptedHub([HubUnreachableError("blip")])
    run_tick(
        enforcer=enforcer,
        hub=hub2,
        state=state,
        managed_users=["alice"],
        agent_version="0.1.0",
        tz_name="UTC",
    )

    assert daemon.balance_s == balance_after_first_tick


def test_offline_grace_and_cap_defaults_are_positive():
    # Sanity check the module-level constants haven't been accidentally
    # zeroed -- the tests above rely on a real grace window existing.
    assert DEFAULT_OFFLINE_GRACE_S > 0
    assert DEFAULT_OFFLINE_CAP_S > 0
