"""Wire types shared by the hub and the agent.

PLAN reference: "API" — "All wire shapes are Pydantic models in a shared
core/ package imported by both hub and agent, so the contract cannot drift."

These models intentionally mirror the JSON shapes in the plan almost
verbatim; if you're changing a field name here, update the plan/API docs too.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class EnforcementMode(str, Enum):
    ENFORCE = "enforce"
    FAIL_CLOSED = "fail_closed"
    OBSERVE = "observe"


class OfflinePolicy(str, Enum):
    OPEN = "open"
    CAPPED = "capped"
    CLOSED = "closed"


class AccountingMode(str, Enum):
    WALLCLOCK = "wallclock"
    PARALLEL = "parallel"


class GrantSource(str, Enum):
    PARENT = "parent"
    LOCAL_TIMEKPRA = "local_timekpra"
    AUTO_CARRYOVER = "auto_carryover"


# --------------------------------------------------------------------------
# Enrollment
# --------------------------------------------------------------------------


class EnrollRequest(BaseModel):
    enrollment_code: str
    hostname: str
    machine_id: str
    os: str
    tz: str
    agent_version: str
    local_users: list[str] = Field(default_factory=list)


class EnrollResponse(BaseModel):
    device_id: str
    device_token: str
    hub_time: str
    next_poll_ms: int


# --------------------------------------------------------------------------
# Sync
# --------------------------------------------------------------------------


class ActiveSpan(BaseModel):
    start: str
    """ISO8601 timestamp, start of the wall-clock window this tick covers."""

    end: str
    """ISO8601 timestamp, end of the wall-clock window this tick covers."""

    burned_s: int
    """Seconds of genuine local activity within [start, end) — i.e. the delta
    fed to `convergence.advance_cumulative`, not just `end - start`."""


class SyncObserved(BaseModel):
    balance_s: int
    spent_day_s: int
    limit_today_s: int
    logged_in: bool
    active: bool


class SyncUserRequest(BaseModel):
    username: str
    day: str
    """The canonical day (YYYY-MM-DD) the agent believes it's reporting for —
    echoed back so the hub can detect an agent that's fallen behind on
    rollover."""

    cumulative_spent_s: int
    active_span: ActiveSpan | None = None
    observed: SyncObserved
    local_grant_s: int = 0
    policy_version_applied: int = 0


class SyncRequest(BaseModel):
    agent_time: str
    tz: str
    ntp_synced: bool
    agent_version: str
    users: list[SyncUserRequest]


class SyncUserResponse(BaseModel):
    username: str
    global_spent_s: int
    remote_spent_s: int
    effective_limit_today_s: int
    effective_week_limit_s: int
    effective_month_limit_s: int
    enforcement: EnforcementMode
    suppressed: bool = False
    policy_version: int
    policy: PolicyPayload | None = None


class SyncResponse(BaseModel):
    hub_time: str
    day: str
    iso_week: str
    month: str
    next_poll_ms: int
    users: list[SyncUserResponse]


# --------------------------------------------------------------------------
# Policy
# --------------------------------------------------------------------------


class AllowedHourInterval(BaseModel):
    hour: int = Field(ge=0, le=23)
    start_min: int = Field(ge=0, le=59)
    end_min: int = Field(ge=0, le=60)
    unaccounted: bool = False


class PolicyPayload(BaseModel):
    version: int
    daily_limits_s: list[int] = Field(min_length=7, max_length=7)
    """Seconds, index 0 = Monday .. index 6 = Sunday (ISO weekday - 1)."""

    allowed_hours: dict[str, list[AllowedHourInterval]] = Field(default_factory=dict)
    """Keyed by ISO weekday as a string, "1".."7"."""

    allowed_weekdays: list[str] = Field(default_factory=list)
    weekly_limit_s: int
    monthly_limit_s: int
    lockout_type: str = "lock"
    wake_from: str | None = None
    wake_to: str | None = None
    track_inactive: bool = False
    note: str = ""


# --------------------------------------------------------------------------
# Events (fire-and-forget, batched)
# --------------------------------------------------------------------------


class EventKind(str, Enum):
    LOCAL_POLICY_DRIFT = "local_policy_drift"
    CLOCK_SKEW = "clock_skew"
    DBUS_LOST = "dbus_lost"
    ENFORCEMENT_FAILED = "enforcement_failed"
    AGENT_STARTED = "agent_started"
    CORRECTION_APPLIED = "correction_applied"


class Event(BaseModel):
    kind: EventKind
    username: str | None = None
    ts: str
    detail: dict = Field(default_factory=dict)


class EventBatch(BaseModel):
    events: list[Event]


# --------------------------------------------------------------------------
# Parent-facing API (subset needed for Phase 1)
# --------------------------------------------------------------------------


class GrantCreate(BaseModel):
    seconds: int
    reason: str = ""


class UserSummary(BaseModel):
    username: str
    display_name: str
    accounting_mode: AccountingMode
    today_global_spent_s: int
    today_effective_limit_s: int
    devices_active_today: list[str] = Field(default_factory=list)
