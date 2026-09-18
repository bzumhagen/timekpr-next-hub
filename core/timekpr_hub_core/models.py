"""Wire types shared by the hub and the agent.

PLAN reference: "API" — "All wire shapes are Pydantic models in a shared
core/ package imported by both hub and agent, so the contract cannot drift."

These models intentionally mirror the JSON shapes in the plan almost
verbatim; if you're changing a field name here, update the plan/API docs too.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, field_validator


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


class LocalPolicySnapshot(BaseModel):
    """A device's own currently-configured limits for one local user, sent
    at enroll time (Phase 5a) so a brand-new hub user's policy is seeded
    from what's actually configured on the first device to report it,
    rather than always starting from the hub's 1h/day placeholder default."""

    daily_limits_s: list[int] = Field(min_length=7, max_length=7)
    weekly_limit_s: int
    monthly_limit_s: int
    allowed_weekdays: list[str] = Field(default_factory=list)


class EnrollRequest(BaseModel):
    # max_length values mirror the column sizes in hub/timekpr_hub/db/models.py
    # (Device.hostname/machine_id/agent_version/tz, UserAlias.local_username)
    # -- unbounded input here used to reach the DB and 500 as a raw
    # asyncpg.StringDataRightTruncationError instead of a 422 (docs/best-
    # practices-review.md).
    enrollment_code: str = Field(max_length=16)
    hostname: str = Field(max_length=255)
    machine_id: str = Field(max_length=64)
    os: str = Field(max_length=255)
    tz: str = Field(max_length=64)
    agent_version: str = Field(max_length=32)
    local_users: list[str] = Field(default_factory=list)
    local_policies: dict[str, LocalPolicySnapshot] = Field(default_factory=dict)
    """Keyed by local username, a subset of `local_users`. Only used to
    seed a policy for a username the hub doesn't already know -- ignored
    for one that merges into an existing hub user (see
    hub/timekpr_hub/api/enroll.py)."""


class EnrollResponse(BaseModel):
    device_id: str
    device_token: str
    hub_time: str
    hub_tz: str
    next_poll_ms: int
    new_users: list[str] = Field(default_factory=list)
    """Which of `local_users` got a freshly-created hub `User` (vs. merged
    into one the hub already knew) -- lets `enroll` tell a parent "new hub
    user, policy seeded from this device" vs. "joined an existing user"."""
    policies: dict[str, PolicyPayload] = Field(default_factory=dict)
    """Each reported user's current effective policy, so `enroll` can print
    a "this device: Xh/day -> hub: Yh/day" diff instead of enrolling silently."""
    rebound: bool = False
    """True when this enrollment matched an existing device by machine_id
    and rotated its token in place, rather than creating a new device row --
    see hub/timekpr_hub/api/enroll.py. Lets `enroll` tell a parent "re-bound
    to your existing device" instead of quietly forking a second history for
    the same machine after a reinstall."""
    previously_enrolled_at: str | None = None
    """ISO8601 -- when the rebound device was originally enrolled. None
    unless `rebound` is True."""


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


class ActivityState(str, Enum):
    DRAINING = "draining"
    """This tick burned real local activity -- the balance is actively moving."""

    IDLE = "idle"
    """Logged in, but nothing was burned this tick (locked screen, AFK)."""

    LOGGED_OUT = "logged_out"


class SyncObserved(BaseModel):
    balance_s: int
    spent_day_s: int
    limit_today_s: int
    logged_in: bool
    active: bool
    activity_state: ActivityState = ActivityState.LOGGED_OUT


class SyncUserRequest(BaseModel):
    username: str
    day: str
    """The canonical day (YYYY-MM-DD) the agent believes it's reporting for —
    echoed back so the hub can detect an agent that's fallen behind on
    rollover."""

    cumulative_spent_s: int
    active_spans: list[ActiveSpan] = Field(default_factory=list)
    """Usually this tick's single span, but may carry more than one: any
    spans a previous tick couldn't reach the hub with are buffered
    (agent's UserState.pending_spans) and resent here rather than lost from
    the wall-clock union forever -- insert_activity_interval is idempotent
    on (device_id, window_end_ts), so replaying an already-recorded span is
    a no-op."""
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
    hub_tz: str
    day: str
    iso_week: str
    month: str
    next_poll_ms: int
    users: list[SyncUserResponse]


# --------------------------------------------------------------------------
# Policy
# --------------------------------------------------------------------------


class LockoutType(str, Enum):
    """Mirrors timekpr's own restriction-type constants
    (`common/constants/constants.py` TK_CTRL_RES_*) -- the six values
    `setLockoutType` accepts. timekpr itself does not validate this at all
    (any string is written straight to LOCKOUT_TYPE); the hub validates so a
    typo can't reach the config file."""

    LOCK = "lock"
    SUSPEND = "suspend"
    SUSPEND_WAKE = "suspendwake"
    TERMINATE = "terminate"
    KILL = "kill"
    SHUTDOWN = "shutdown"


class AllowedHourInterval(BaseModel):
    hour: int = Field(ge=0, le=23)
    start_min: int = Field(ge=0, le=59)
    end_min: int = Field(ge=0, le=60)
    unaccounted: bool = False


class PlayTimeActivity(BaseModel):
    """One PLAYTIME_ACTIVITY_NNN entry. `mask` is a regex matched against a
    process's executable path (and, if
    TIMEKPR_PLAYTIME_ENHANCED_ACTIVITY_MONITOR_ENABLED, its cmdline) --
    `[`/`]` are stripped by timekpr itself since they delimit the
    description (server/user/playtime.py), so a mask containing either is
    rejected here rather than silently mangled."""

    mask: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=255)

    @field_validator("mask")
    @classmethod
    def _no_bracket_chars(cls, value: str) -> str:
        if "[" in value or "]" in value:
            raise ValueError(
                "mask may not contain '[' or ']' -- they delimit the description in timekpr's own format"
            )
        return value


class PlayTimePayload(BaseModel):
    """The `[<user>.PLAYTIME]` config section -- a screen-time sub-budget for
    specific processes, nested inside the normal daily limit. See
    server/user/playtime.py and README.md's PlayTime section."""

    enabled: bool = False
    override_enabled: bool = False
    """When true, normal time only ticks while a matched activity is
    running, and PlayTime's own limits below are ignored entirely
    (server/user/userdata.py userActiveEffective = userActivePT)."""
    unaccounted_intervals_enabled: bool = True
    """Whether PlayTime activities may run (and count toward PlayTime) during
    an unaccounted ("!") hour; if false they're killed outright during one."""
    allowed_weekdays: list[str] = Field(default_factory=lambda: ["1", "2", "3", "4", "5", "6", "7"])
    daily_limits_s: list[int] = Field(min_length=7, max_length=7, default_factory=lambda: [0] * 7)
    activities: list[PlayTimeActivity] = Field(default_factory=list)

    @field_validator("daily_limits_s")
    @classmethod
    def _each_day_within_a_day(cls, value: list[int]) -> list[int]:
        if any(v < 0 or v > 86400 for v in value):
            raise ValueError("each PlayTime daily limit must be between 0 and 86400 seconds")
        return value


class PolicyPayload(BaseModel):
    version: int
    daily_limits_s: list[int] = Field(min_length=7, max_length=7)
    """Seconds, index 0 = Monday .. index 6 = Sunday (ISO weekday - 1)."""

    allowed_hours: dict[str, list[AllowedHourInterval]] = Field(default_factory=dict)
    """Keyed by ISO weekday as a string, "1".."7". An empty dict/day means
    "unrestricted" at the hub layer (see core/timekpr_hub_core/allowed_hours.py
    `unrestricted()`) -- it must never be pushed to timekpr as-is, since
    timekpr's own semantics for an absent hour is "forbidden", not "allowed"."""

    allowed_weekdays: list[str] = Field(default_factory=list)
    weekly_limit_s: int
    monthly_limit_s: int
    lockout_type: LockoutType = LockoutType.LOCK
    wake_from: str | None = None
    wake_to: str | None = None
    track_inactive: bool = False
    hide_tray_icon: bool = False
    playtime: PlayTimePayload = Field(default_factory=PlayTimePayload)
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
    seconds: int = Field(ge=-86400, le=86400)
    reason: str = Field(default="", max_length=255)
    day: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    """Canonical day (YYYY-MM-DD) this grant applies to. None means today in
    the hub's timezone -- unchanged behavior for every existing caller, which
    always meant today before dated grants existed. Setting it lets a parent
    adjust a *future* day ("you lose 30 minutes tomorrow") without touching
    the standing policy. For cancelling a day outright, prefer a
    DayOverride instead of a large negative grant: a grant's stored
    second-count stops cancelling correctly the moment that weekday's
    standing limit changes; an override replaces the base outright."""


class DayOverrideCreate(BaseModel):
    """PUT /users/{u}/day-override -- an absolute per-date limit, replacing
    (not adding to) the policy's standing limit for that weekday. 0 is a
    full moratorium; see services/limits.py::set_day_override."""

    day: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    limit_seconds: int = Field(ge=0, le=86400)
    reason: str = Field(default="", max_length=255)


class GateReleaseCreate(BaseModel):
    """POST /users/{u}/gate-release -- records that a gated day's
    precondition was met for one date. Releasing an already-released day is
    a no-op that just refreshes who/why."""

    day: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    note: str = Field(default="", max_length=255)


class UserSettingsUpdate(BaseModel):
    """PUT /users/{u}/settings -- the hub-only per-user knobs that never
    reach `PolicyPayload` or a device: which weekdays are approval-gated, and
    the accounting mode. Deliberately NOT part of PolicyUpdate/update_policy
    -- these have no policy version, no device push, and their own single
    save button in the UI (see docs/best-practices-review.md's "tab-scoped
    Apply" anti-pattern this avoids by not sharing a save action with the
    policy editor)."""

    gated_weekdays: list[str] = Field(default_factory=list)
    accounting_mode: AccountingMode = AccountingMode.WALLCLOCK

    @field_validator("gated_weekdays")
    @classmethod
    def _valid_weekday_tokens(cls, value: list[str]) -> list[str]:
        if any(v not in {"1", "2", "3", "4", "5", "6", "7"} for v in value):
            raise ValueError("gated_weekdays entries must be '1'..'7' (Mon..Sun)")
        return value


class UserSummary(BaseModel):
    username: str
    display_name: str
    accounting_mode: AccountingMode
    today_global_spent_s: int
    today_effective_limit_s: int
    devices_active_today: list[str] = Field(default_factory=list)
    activity_state: ActivityState = ActivityState.LOGGED_OUT
    """The most recent non-stale device's activity_state for this user (see
    hub/timekpr_hub/api/ui.py's staleness rule) -- degrades to LOGGED_OUT once
    no device has checked in recently."""
    as_of: str | None = None
    """ISO8601 timestamp of the sync that produced today_global_spent_s --
    lets a viewer (the UI) extrapolate forward while DRAINING instead of
    only ever showing a value up to one poll interval stale."""
    gated_today: bool = False
    """True when today is one of this user's `gated_weekdays` AND it hasn't
    been released yet -- the state `today_effective_limit_s` is already 0
    for (see services/limits.py::combine_limit). False both when today isn't
    a gated weekday at all and when it's gated but already released, so a
    caller wanting "is this an approval needed day" needs `gate_released_today` too."""
    gate_released_today: bool = False
    """True when today is a gated weekday AND a GateRelease row exists for
    it. Meaningless (always False) when today isn't gated at all -- check
    alongside `gated_today` or the user's `gated_weekdays`, not alone."""


class PolicyUpdate(BaseModel):
    """A parent-initiated change to a user's policy -- PUT /users/{u}/policy.
    Always creates a new `Policy` version rather than mutating one in place
    (see services/policy.py::update_policy). Every field `PolicyPayload`
    carries is settable here -- the hub's own advanced editor is the last
    caller that used to need to "carry forward" the fields below unedited."""

    daily_limits_s: list[int] = Field(min_length=7, max_length=7)
    weekly_limit_s: int = Field(ge=0, le=7 * 86400)
    monthly_limit_s: int = Field(ge=0, le=31 * 86400)
    allowed_weekdays: list[str] = Field(default_factory=lambda: ["1", "2", "3", "4", "5", "6", "7"])
    allowed_hours: dict[str, list[AllowedHourInterval]] = Field(default_factory=dict)
    lockout_type: LockoutType = LockoutType.LOCK
    wake_from: str | None = Field(default=None, pattern=r"^([01]?[0-9]|2[0-3])$")
    wake_to: str | None = Field(default=None, pattern=r"^([01]?[0-9]|2[0-3])$")
    track_inactive: bool = False
    hide_tray_icon: bool = False
    playtime: PlayTimePayload = Field(default_factory=PlayTimePayload)
    note: str = Field(default="", max_length=500)

    @field_validator("daily_limits_s")
    @classmethod
    def _each_day_within_a_day(cls, value: list[int]) -> list[int]:
        if any(v < 0 or v > 86400 for v in value):
            raise ValueError("each daily limit must be between 0 and 86400 seconds")
        return value

    @field_validator("allowed_weekdays")
    @classmethod
    def _valid_weekday_tokens(cls, value: list[str]) -> list[str]:
        if any(v not in {"1", "2", "3", "4", "5", "6", "7"} for v in value):
            raise ValueError("allowed_weekdays entries must be '1'..'7' (Mon..Sun)")
        return value
