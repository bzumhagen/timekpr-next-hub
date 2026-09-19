"""Agent persisted state.

Persisted at /var/lib/timekpr-hub-agent/state.json (atomic
write+fsync+rename), holding the last-known policy, L, R_d, G,
applied_offset, cum_local and day -- everything the agent needs to keep
enforcing correctly while the hub is unreachable.
"""

from __future__ import annotations

import dataclasses
import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

DEFAULT_STATE_PATH = Path("/var/lib/timekpr-hub-agent/state.json")


@dataclass
class UserState:
    day: str = ""
    cum_local_s: int = 0
    raw_prev_s: int = 0
    applied_offset_s: int = 0
    policy_version_applied: int = 0
    policy_revision_applied: str = ""
    """The `policy_revision` last successfully applied (see
    `SyncUserRequest.policy_revision_applied`'s docstring for why this is a
    separate field from the int version above rather than a replacement for
    it -- both are kept and both only advance on a successful push)."""
    last_enforcement: str = ""  # "enforce", "observe", or "revoked" -- only used to log on transition

    # Cached last-known values from the hub, used while offline (PLAN
    # "Offline / hub-unreachable behavior"). Wall-clock (epoch seconds), not
    # time.monotonic(): monotonic's epoch is arbitrary and resets on
    # reboot, which used to make "seconds since contact" go deeply negative
    # after a restart -- in_grace would then read True forever and the
    # agent would silently stay unenforced while genuinely offline.
    last_effective_limit_today_s: int = 0
    last_global_spent_s: int = 0
    last_hub_contact_utc: float = 0.0
    # cum_local_s at the moment of last_hub_contact_utc -- lets offline
    # enforcement credit local activity since contact without ever
    # refunding it (see main.py's _apply_offline_policy).
    cum_local_at_contact_s: int = 0

    # Wall-clock end of the last tick's active_span (epoch seconds), so the
    # next tick's span can start exactly where the last one ended instead of
    # being fabricated as `now - burned_s` (which reaches backwards past the
    # previous span whenever burned_s exceeds the real gap between ticks,
    # e.g. after a delayed tick or a suspend/resume -- the hub's range_agg
    # union then silently swallows the overlap). 0.0 means "no prior span",
    # i.e. don't backdate past this tick's own start.
    last_tick_utc: float = 0.0
    # Spans that failed to reach the hub (sync raised), buffered so the next
    # successful sync replays them instead of losing that activity from the
    # wall-clock union forever -- insert_activity_interval is idempotent on
    # (device_id, window_end_ts), so replay is always safe. Capped so a long
    # outage can't grow state.json without bound; oldest entries are dropped
    # first (cumulative_spent_s, the absolute counter, is unaffected either
    # way -- only the union-mode display would have under-counted).
    pending_spans: list[dict] = field(default_factory=list)


@dataclass
class AgentState:
    users: dict[str, UserState] = field(default_factory=dict)
    # Cached from the hub's last EnrollResponse/SyncResponse. The household
    # timezone lives on the hub (HUB_TZ), not on this device -- this is what
    # lets the agent compute its own canonical-day rollover correctly
    # (main.py's _canonical_day_str) both online and, using the cached
    # value, while offline. Empty until the first successful sync.
    hub_tz: str = ""

    def user(self, username: str) -> UserState:
        if username not in self.users:
            self.users[username] = UserState()
        return self.users[username]


def load(path: Path = DEFAULT_STATE_PATH) -> AgentState:
    if not path.exists():
        return AgentState()
    try:
        raw = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        # Corrupted state file -- start fresh rather than crash-looping the
        # agent. Worst case this looks like a canonical-day rollover on the
        # next tick, which is a safe (if slightly wasteful) fallback.
        return AgentState()

    known_fields = {f.name for f in dataclasses.fields(UserState)}
    state = AgentState(hub_tz=raw.get("hub_tz", "") if isinstance(raw.get("hub_tz"), str) else "")
    for username, user_raw in raw.get("users", {}).items():
        if not isinstance(user_raw, dict):
            continue
        # Drop unknown keys rather than TypeError on them -- an older or
        # newer agent version's state.json (e.g. the now-removed
        # last_hub_contact_monotonic) must never crash-loop the agent after
        # an upgrade/downgrade. Missing keys still take UserState's defaults.
        filtered = {k: v for k, v in user_raw.items() if k in known_fields}
        try:
            state.users[username] = UserState(**filtered)
        except TypeError:
            state.users[username] = UserState()
    return state


def save(state: AgentState, path: Path = DEFAULT_STATE_PATH) -> None:
    """Atomic write: write to a temp file in the same directory, fsync, then
    rename over the target. Never leaves a partially-written state.json even
    if the process is killed mid-write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"hub_tz": state.hub_tz, "users": {name: asdict(u) for name, u in state.users.items()}}

    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=".state-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        # Also fsync the containing directory -- without this, the rename
        # itself can be lost on a power cut even though the file content
        # was durable, on filesystems that don't order directory entry
        # updates with file writes.
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
