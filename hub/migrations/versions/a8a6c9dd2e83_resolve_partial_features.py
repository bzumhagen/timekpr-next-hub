"""resolve partially-implemented features: device approval, events/alerts,
local-grant sources, parent TOTP, ntp/raw-drift telemetry

Revision ID: a8a6c9dd2e83
Revises: abfbdcbbb0e4
Create Date: 2026-09-19 17:29:52.408989

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a8a6c9dd2e83"
down_revision: str | Sequence[str] | None = "abfbdcbbb0e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Device approval: enroll.py has set every device to 'active' since the
    # very first schema (a parent-minted enrollment code is itself the
    # approval), so 'pending' was never reachable. Existing rows are all
    # already 'active' or 'revoked'.
    op.drop_constraint("ck_devices_status", "devices", type_="check")
    op.create_check_constraint("ck_devices_status", "devices", "status IN ('active', 'revoked')")
    op.alter_column("devices", "status", server_default="active")

    # Events/alerts: wire types with no producer on either side and a
    # destination table nothing ever read.
    op.drop_table("alerts")

    # Local-grant adoption + carryover: the columns/enum values existed but
    # nothing ever read local_grant_s or wrote a non-'parent' Grant.source.
    op.drop_column("users", "auto_adopt_local_grants")
    op.drop_constraint("ck_grants_source", "grants", type_="check")
    op.create_check_constraint("ck_grants_source", "grants", "source IN ('parent')")

    # Parent TOTP: never built.
    op.drop_column("parents", "totp_secret")

    # NTP/raw-drift telemetry: fed only the events/alerts channel above and
    # (for ntp_synced) was always a hardcoded True from the agent. Clock
    # skew is kept -- see api/sync.py's clock_skew_ms -- and now computed
    # for real, so it stays; widened to BigInteger since it's now actually
    # populated and a wrong system clock (or a compressed-time test
    # harness) can exceed int32's +-24.8 day range in milliseconds.
    op.alter_column("devices", "clock_skew_ms", type_=sa.BigInteger())
    op.drop_column("devices", "ntp_synced")
    op.drop_column("usage_counters", "raw_balance_s")
    op.drop_column("usage_counters", "raw_limit_today_s")

    # Device.hostname/os_info/tz/token_prefix: write-only. `name` already
    # holds the same value as `hostname` did (both set from the enroll
    # request's own hostname field), so nothing is lost by dropping the
    # separate column; os_info/tz/token_prefix had no reader at all.
    op.drop_column("devices", "hostname")
    op.drop_column("devices", "os_info")
    op.drop_column("devices", "tz")
    op.drop_column("devices", "token_prefix")


def downgrade() -> None:
    op.add_column("devices", sa.Column("token_prefix", sa.String(length=12), nullable=True))
    op.add_column("devices", sa.Column("tz", sa.String(length=64), nullable=True))
    op.add_column("devices", sa.Column("os_info", sa.String(length=255), nullable=True))
    op.add_column("devices", sa.Column("hostname", sa.String(length=255), nullable=True))

    op.add_column("usage_counters", sa.Column("raw_limit_today_s", sa.BigInteger(), nullable=True))
    op.add_column("usage_counters", sa.Column("raw_balance_s", sa.BigInteger(), nullable=True))
    op.add_column("devices", sa.Column("ntp_synced", sa.Boolean(), nullable=True))
    op.alter_column("devices", "clock_skew_ms", type_=sa.Integer())

    op.add_column("parents", sa.Column("totp_secret", sa.String(length=64), nullable=True))

    op.drop_constraint("ck_grants_source", "grants", type_="check")
    op.create_check_constraint(
        "ck_grants_source", "grants", "source IN ('parent', 'local_timekpra', 'auto_carryover')"
    )
    op.add_column(
        "users",
        sa.Column("auto_adopt_local_grants", sa.Boolean(), server_default="true", nullable=False),
    )

    op.create_table(
        "alerts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=True),
        sa.Column("device_id", sa.UUID(), nullable=True),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("severity", sa.String(length=16), server_default="info", nullable=False),
        sa.Column("message", sa.String(length=500), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("acked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["device_id"], ["devices.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.alter_column("devices", "status", server_default="pending")
    op.drop_constraint("ck_devices_status", "devices", type_="check")
    op.create_check_constraint("ck_devices_status", "devices", "status IN ('pending', 'active', 'revoked')")
