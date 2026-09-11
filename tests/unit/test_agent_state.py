"""Phase 1 "reboot survival" regression tests for agent state persistence.

PLAN reference: docs/best-practices-review.md's "agent's offline grace
timer uses time.monotonic()" finding, and the unknown-key crash-loop risk
it also names.
"""

from __future__ import annotations

import json

from timekpr_hub_agent.state import AgentState, UserState, load, save


def test_load_missing_file_returns_empty_state(tmp_path):
    state = load(tmp_path / "does-not-exist.json")
    assert state.users == {}
    assert state.hub_tz == ""


def test_load_corrupted_json_starts_fresh_instead_of_crashing(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not valid json")
    state = load(path)
    assert state.users == {}


def test_load_ignores_unknown_keys_instead_of_crashing(tmp_path):
    """A field removed between agent versions (e.g. the retired
    last_hub_contact_monotonic) must never TypeError -- that would
    crash-loop the agent forever after an upgrade, since state.json is
    reloaded on every restart."""
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "users": {
                    "alice": {
                        "day": "2026-01-01",
                        "cum_local_s": 100,
                        "last_hub_contact_monotonic": 12345.0,  # removed field
                        "some_future_field": "not yet invented",
                    }
                }
            }
        )
    )
    state = load(path)
    assert state.users["alice"].day == "2026-01-01"
    assert state.users["alice"].cum_local_s == 100
    # Fields not present in the old file fall back to UserState's defaults.
    assert state.users["alice"].last_hub_contact_utc == 0.0


def test_load_skips_non_dict_user_entries(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"users": {"alice": "not a dict"}}))
    state = load(path)
    assert "alice" not in state.users


def test_save_and_load_round_trip_including_hub_tz(tmp_path):
    path = tmp_path / "sub" / "state.json"
    original = AgentState(hub_tz="America/Denver")
    original.user("alice").cum_local_s = 500
    original.user("alice").last_hub_contact_utc = 1_700_000_000.0

    save(original, path)
    loaded = load(path)

    assert loaded.hub_tz == "America/Denver"
    assert loaded.users["alice"].cum_local_s == 500
    assert loaded.users["alice"].last_hub_contact_utc == 1_700_000_000.0


def test_save_is_atomic_no_leftover_tmp_file_on_success(tmp_path):
    path = tmp_path / "state.json"
    save(AgentState(), path)
    leftovers = list(tmp_path.glob(".state-*.tmp"))
    assert leftovers == []
    assert path.exists()


def test_user_creates_default_state_on_first_access():
    state = AgentState()
    assert state.user("bob") == UserState()
    assert "bob" in state.users
