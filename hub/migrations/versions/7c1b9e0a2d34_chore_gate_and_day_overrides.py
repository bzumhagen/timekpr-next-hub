"""approval gate (users.gated_weekdays) + day_overrides + gate_releases

Revision ID: 7c1b9e0a2d34
Revises: 5fe40acd11d9
Create Date: 2026-09-12 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "7c1b9e0a2d34"
down_revision: str | Sequence[str] | None = "5fe40acd11d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The recurring half of the approval gate, hub-only like the other knobs on
    # this table (accounting_mode, offline_policy, ...). Default [] means no
    # existing user's behavior changes on upgrade.
    op.add_column(
        "users",
        sa.Column(
            "gated_weekdays_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
    )

    # Absolute per-date limit: 0 is a full moratorium, any other value a
    # reduced day. Unique per (user, day) -- setting one twice replaces it.
    op.create_table(
        "day_overrides",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("limit_seconds", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "limit_seconds >= 0 AND limit_seconds <= 86400", name="ck_day_overrides_limit_seconds"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "day", name="uq_day_overrides_user_day"),
    )
    op.create_index("ix_day_overrides_user_day", "day_overrides", ["user_id", "day"])

    # The per-date exception to a gated weekday. Absence of a row is the
    # gate, so nothing here needs expiring or sweeping.
    op.create_table(
        "gate_releases",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("released_by", sa.String(length=64), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "day", name="uq_gate_releases_user_day"),
    )
    op.create_index("ix_gate_releases_user_day", "gate_releases", ["user_id", "day"])


def downgrade() -> None:
    op.drop_index("ix_gate_releases_user_day", table_name="gate_releases")
    op.drop_table("gate_releases")
    op.drop_index("ix_day_overrides_user_day", table_name="day_overrides")
    op.drop_table("day_overrides")
    op.drop_column("users", "gated_weekdays_json")
