"""Wire types shared by the hub and the agent.

Every wire shape is a Pydantic model in this shared package, imported by
both the hub and the agent, so the contract cannot drift between them.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class EnforcementMode(str, Enum):
    ENFORCE = "enforce"
    OBSERVE = "observe"


class OfflinePolicy(str, Enum):
    OPEN = "open"
    CAPPED = "capped"
    CLOSED = "closed"


class AccountingMode(str, Enum):
    WALLCLOCK = "wallclock"
    PARALLEL = "parallel"


# --------------------------------------------------------------------------
# Enrollment
# --------------------------------------------------------------------------


class LocalPolicySnapshot(BaseModel):
    """A device's own currently-configured limits for one local user, sent
    at enroll time so a brand-new hub user's policy is seeded
    from what's actually configured on the first device to report it,
    rather than always starting from the hub's 1h/day placeholder default."""

    daily_limits_s: list[int] = Field(min_length=7, max_length=7)
    weekly_limit_s: int
    monthly_limit_s: int
    allowed_weekdays: list[str] = Field(default_factory=list)


class EnrollRequest(BaseModel):
    # max_length values mirror the column sizes in hub/timekpr_hub/db/models.py
    # (Device.machine_id/agent_version, UserAlias.local_username -- `name` is
    # set from `hostname` and shares its bound) -- unbounded input here would
    # otherwise reach the DB and 500 as a raw asyncpg.StringDataRightTruncationError
    # instead of a 422.
    enrollment_code: str = Field(max_length=16)
    hostname: str = Field(max_length=255)
    machine_id: str = Field(max_length=64)
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
    into one the hub already knew) -- lets `enroll` tell an admin "new hub
    user, policy seeded from this device" vs. "joined an existing user"."""
    policies: dict[str, PolicyPayload] = Field(default_factory=dict)
    """Each reported user's current effective policy, so `enroll` can print
    a "this device: Xh/day -> hub: Yh/day" diff instead of enrolling silently."""
    rebound: bool = False
    """True when this enrollment matched an existing device by machine_id
    and rotated its token in place, rather than creating a new device row --
    see hub/timekpr_hub/api/enroll.py. Lets `enroll` tell an admin "re-bound
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
    cumulative_spent_s: int
    active_spans: list[ActiveSpan] = Field(default_factory=list)
    """Usually this tick's single span, but may carry more than one: any
    spans a previous tick couldn't reach the hub with are buffered
    (agent's UserState.pending_spans) and resent here rather than lost from
    the wall-clock union forever -- insert_activity_interval is idempotent
    on (device_id, window_end_ts), so replaying an already-recorded span is
    a no-op."""
    observed: SyncObserved
    policy_version_applied: int = 0
    policy_revision_applied: str | None = None
    """The `timekpr_hub_core.effective_policy.policy_revision` value last
    successfully applied, echoed back so the hub knows whether today's
    effective payload (standing policy + any one-day allowed-hours override)
    still matches what's on the device. `None` -- not `""` -- specifically
    means "this agent predates this field" (Pydantic only produces `None`
    when the key is absent from the request body): such an agent must be
    served the *standing* payload only, gated on `policy_version_applied`
    exactly as before, never the effective one -- otherwise it would apply
    an hours override it can never be told to revert (it only ever echoes
    the int version, which an expiring override does not change)."""


class SyncRequest(BaseModel):
    agent_time: str
    agent_version: str
    users: list[SyncUserRequest]


class SyncUserResponse(BaseModel):
    username: str
    global_spent_s: int
    effective_limit_today_s: int
    effective_week_limit_s: int
    effective_month_limit_s: int
    enforcement: EnforcementMode
    policy_version: int
    policy_revision: str = ""
    """See `SyncUserRequest.policy_revision_applied`. Defaulted so the
    unmapped-user (observe-only) branch of `/sync` needs no change: an
    unmapped user is never pushed a policy either way."""
    policy: PolicyPayload | None = None
    offline_policy: OfflinePolicy = OfflinePolicy.CAPPED
    """What the agent should do once `offline_grace_s` has elapsed since its
    last successful sync -- see `User.offline_policy` and the agent's
    `_apply_offline_policy`. Defaulted to `capped` (today's only behavior
    before this field existed) so the unmapped-user branch needs no change."""
    offline_grace_s: int = 900
    offline_cap_s: int = 1800


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
# Admin-facing API
# --------------------------------------------------------------------------


class GrantCreate(BaseModel):
    seconds: int = Field(ge=-86400, le=86400)
    reason: str = Field(default="", max_length=255)
    day: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    """Canonical day (YYYY-MM-DD) this grant applies to. None means today in
    the hub's timezone -- unchanged behavior for every existing caller, which
    always meant today before dated grants existed. Setting it lets an admin
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


class DayHourOverrideCreate(BaseModel):
    """PUT /users/{u}/day-hours -- a one-day replacement for the policy's
    standing allowed time-of-day window, e.g. "today, allow 12:00-20:00
    instead of the usual 15:00-20:00" or "any time today". Kept deliberately
    separate from `DayOverrideCreate` (which replaces the day's *limit*, in
    seconds) -- this replaces *when* the day's time may be used, not *how
    much* of it there is; the two compose (see
    `timekpr_hub.services.limits.combine_limit`'s docstring on gates
    composing with overrides for the same reasoning).

    Unlike `DayOverrideCreate` and `GrantCreate`, this DOES eventually reach
    the device -- see `timekpr_hub_core.effective_policy` for why an hours
    change can't stay hub-side the way a seconds change can."""

    day: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    mode: Literal["unrestricted", "window"]
    from_min: int = Field(default=0, ge=0, le=24 * 60)
    to_min: int = Field(default=24 * 60, ge=0, le=24 * 60)
    unaccounted: bool = False
    """Only meaningful with mode="window": timekpr's "!" unaccounted hours
    -- allowed, but not counted against the daily limit. Ignored for
    "unrestricted", which always writes the plain all-24-hours form."""
    reason: str = Field(default="", max_length=255)

    @model_validator(mode="after")
    def _window_is_ordered(self) -> DayHourOverrideCreate:
        if self.mode == "window" and self.from_min >= self.to_min:
            raise ValueError("'from' must be earlier than 'to'")
        return self


class GateReleaseCreate(BaseModel):
    """POST /users/{u}/gate-release -- records that a gated day's
    precondition was met for one date. Releasing an already-released day is
    a no-op that just refreshes who/why."""

    day: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    note: str = Field(default="", max_length=255)


class UserSettingsUpdate(BaseModel):
    """PUT /users/{u}/settings -- the hub-only per-user knobs that never
    reach `PolicyPayload` or a device: which weekdays are approval-gated, the
    accounting mode, and the offline-grace policy. Deliberately NOT part of
    PolicyUpdate/update_policy -- these have no policy version, no device
    push, and their own single save button in the UI, rather than sharing a
    save action with the policy editor."""

    gated_weekdays: list[str] = Field(default_factory=list)
    accounting_mode: AccountingMode = AccountingMode.WALLCLOCK
    offline_policy: OfflinePolicy = OfflinePolicy.CAPPED
    offline_grace_s: int = Field(default=900, ge=0, le=7 * 86400)
    offline_cap_s: int = Field(default=1800, ge=0, le=7 * 86400)

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
    """An admin-initiated change to a user's policy -- PUT /users/{u}/policy.
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
