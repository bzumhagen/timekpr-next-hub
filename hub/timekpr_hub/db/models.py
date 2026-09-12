"""SQLAlchemy ORM models.

PLAN reference: "Data model". Phase 1 scope only (see CHECKLIST.md): the
tables needed for enrollment, the daily pooled budget, and bonus-time grants.
`alerts` and full `audit_log` detail are deferred to Phase 2 per the plan's
milestone breakdown, but the tables are included now (empty of application
logic) since they're cheap to have and avoid a disruptive migration later.

Everything derived (global/week/month spent, effective limits) is computed
from `usage_counters` + `activity_intervals`, never stored directly — see
`services/aggregate.py`.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSTZRANGE, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


# --------------------------------------------------------------------------
# Parents (household admins)
# --------------------------------------------------------------------------


class Parent(Base):
    __tablename__ = "parents"

    id: Mapped[uuid.UUID] = _uuid_pk()
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    totp_secret: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ParentSession(Base):
    __tablename__ = "parent_sessions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    parent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("parents.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# --------------------------------------------------------------------------
# Users (the children being managed) and their device aliases
# --------------------------------------------------------------------------


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _uuid_pk()
    canonical_username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    # Deliberately NOT a hard ForeignKey: policies.user_id -> users.id already
    # establishes the real relationship, and a FK here would create a
    # circular dependency between the two tables. This is just a "pointer to
    # the current version" convenience column, resolved at the application
    # layer (see services/policy.py).
    current_policy_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    accounting_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="wallclock", server_default="wallclock"
    )
    offline_policy: Mapped[str] = mapped_column(
        String(16), nullable=False, default="capped", server_default="capped"
    )
    offline_grace_s: Mapped[int] = mapped_column(Integer, nullable=False, default=900, server_default="900")
    offline_cap_s: Mapped[int] = mapped_column(Integer, nullable=False, default=1800, server_default="1800")
    auto_adopt_local_grants: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint("accounting_mode IN ('wallclock', 'parallel')", name="ck_users_accounting_mode"),
        CheckConstraint("offline_policy IN ('open', 'capped', 'closed')", name="ck_users_offline_policy"),
    )


class UserAlias(Base):
    """Maps a device's local unix username to a canonical hub user."""

    __tablename__ = "user_aliases"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"))
    local_username: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (UniqueConstraint("device_id", "local_username", name="uq_user_aliases_device_local"),)


# --------------------------------------------------------------------------
# Devices
# --------------------------------------------------------------------------


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    machine_id: Mapped[str] = mapped_column(String(64), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    token_prefix: Mapped[str] = mapped_column(String(12), nullable=False)

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    enforcement: Mapped[str] = mapped_column(
        String(16), nullable=False, default="enforce", server_default="enforce"
    )

    agent_version: Mapped[str | None] = mapped_column(String(32))
    os_info: Mapped[str | None] = mapped_column(String(255))
    tz: Mapped[str | None] = mapped_column(String(64))
    clock_skew_ms: Mapped[int | None] = mapped_column(Integer)
    ntp_synced: Mapped[bool | None] = mapped_column(Boolean)

    enrolled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("status IN ('pending', 'active', 'revoked')", name="ck_devices_status"),
        CheckConstraint("enforcement IN ('enforce', 'observe')", name="ck_devices_enforcement"),
        # Partial: a revoked device's machine_id must not block a *new*
        # device row from later reusing that machine, but any live
        # (pending/active) device is looked up by machine_id at enroll time
        # to rebind instead of forking a second history for the same
        # machine (hub/timekpr_hub/api/enroll.py) -- this is what makes that
        # lookup race-safe under concurrent enrolls of the same machine.
        Index(
            "uq_devices_machine_id_live",
            "machine_id",
            unique=True,
            postgresql_where=text("status <> 'revoked'"),
        ),
    )


# --------------------------------------------------------------------------
# Policy (append-only versions)
# --------------------------------------------------------------------------


class Policy(Base):
    __tablename__ = "policies"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_by: Mapped[str | None] = mapped_column(String(64))

    daily_limits_json: Mapped[list] = mapped_column(JSONB, nullable=False)
    allowed_hours_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    allowed_weekdays_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    weekly_limit_s: Mapped[int] = mapped_column(Integer, nullable=False)
    monthly_limit_s: Mapped[int] = mapped_column(Integer, nullable=False)
    lockout_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default="lock", server_default="lock"
    )
    wake_from: Mapped[str | None] = mapped_column(String(8))
    wake_to: Mapped[str | None] = mapped_column(String(8))
    track_inactive: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    note: Mapped[str | None] = mapped_column(String(500))

    __table_args__ = (UniqueConstraint("user_id", "version", name="uq_policies_user_version"),)


# --------------------------------------------------------------------------
# Usage: the absolute per-device counter (idempotent MAX-merge target) and
# the wall-clock activity spans (for the union/"burn once" computation)
# --------------------------------------------------------------------------


class UsageCounter(Base):
    """The idempotent, MAX-merged absolute cumulative-spent counter per
    (user, device, day). See PLAN "Idempotency: report absolute counters,
    never deltas"."""

    __tablename__ = "usage_counters"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    device_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), primary_key=True
    )
    day: Mapped[date] = mapped_column(Date, primary_key=True)

    spent_seconds: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    raw_balance_s: Mapped[int | None] = mapped_column(BigInteger)
    raw_limit_today_s: Mapped[int | None] = mapped_column(BigInteger)
    activity_state: Mapped[str | None] = mapped_column(String(16))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (Index("ix_usage_counters_user_day", "user_id", "day"),)


class ActivityInterval(Base):
    """One reported wall-clock activity window, for the union computation
    behind "burn once" accounting. Idempotent on (device_id, window_end_ts)."""

    __tablename__ = "activity_intervals"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), nullable=False)
    day: Mapped[date] = mapped_column(Date, nullable=False)
    span: Mapped[str] = mapped_column(TSTZRANGE, nullable=False)
    window_end_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("device_id", "window_end_ts", name="uq_activity_intervals_device_window"),
        Index("ix_activity_intervals_user_day", "user_id", "day"),
    )


# --------------------------------------------------------------------------
# Grants (bonus time, carryover)
# --------------------------------------------------------------------------


class Grant(Base):
    __tablename__ = "grants"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    day: Mapped[date] = mapped_column(Date, nullable=False)
    seconds: Mapped[int] = mapped_column(Integer, nullable=False)  # may be negative
    reason: Mapped[str | None] = mapped_column(String(255))
    source: Mapped[str] = mapped_column(String(24), nullable=False)
    granted_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint("source IN ('parent', 'local_timekpra', 'auto_carryover')", name="ck_grants_source"),
        Index("ix_grants_user_day", "user_id", "day"),
    )


# --------------------------------------------------------------------------
# Alerts / audit log (schema present now, application logic in Phase 2)
# --------------------------------------------------------------------------


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    device_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="info", server_default="info")
    message: Mapped[str] = mapped_column(String(500), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    acked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = _uuid_pk()
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    actor_type: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(64))
    target_id: Mapped[str | None] = mapped_column(String(64))
    before_json: Mapped[dict | None] = mapped_column(JSONB)
    after_json: Mapped[dict | None] = mapped_column(JSONB)
    ip: Mapped[str | None] = mapped_column(String(64))


class EnrollmentCode(Base):
    __tablename__ = "enrollment_codes"

    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # SET NULL (not CASCADE): deleting a device should not delete the
    # historical fact that a code was redeemed and by when -- it just no
    # longer points at a device that exists. Originally had no ondelete
    # action at all (defaulting to RESTRICT), which made every device
    # delete 500 with a foreign-key violation (every enrolled device has a
    # code row pointing at it) -- see migration 3f9a1c7d4e21.
    used_by_device_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("devices.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
