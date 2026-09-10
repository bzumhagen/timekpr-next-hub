"""Agent persisted state.

PLAN reference: "Offline / hub-unreachable behavior" -- "Agent state
persisted at /var/lib/timekpr-hub-agent/state.json (atomic write+fsync+rename),
containing last-known policy, L, R_d, G, applied_offset, cum_local, day."
"""

from __future__ import annotations

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

    # Cached last-known values from the hub, used while offline (PLAN
    # "Offline / hub-unreachable behavior").
    last_effective_limit_today_s: int = 0
    last_global_spent_s: int = 0
    last_hub_contact_monotonic: float = 0.0


@dataclass
class AgentState:
    users: dict[str, UserState] = field(default_factory=dict)

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
        # Corrupted state file (PLAN "Verification" Layer 6: chaos-tests a
        # corrupted state file) -- start fresh rather than crash-looping the
        # agent. Worst case this looks like a canonical-day rollover on the
        # next tick, which is a safe (if slightly wasteful) fallback.
        return AgentState()

    state = AgentState()
    for username, user_raw in raw.get("users", {}).items():
        state.users[username] = UserState(**user_raw)
    return state


def save(state: AgentState, path: Path = DEFAULT_STATE_PATH) -> None:
    """Atomic write: write to a temp file in the same directory, fsync, then
    rename over the target. Never leaves a partially-written state.json even
    if the process is killed mid-write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"users": {name: asdict(u) for name, u in state.users.items()}}

    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=".state-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
