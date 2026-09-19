"""day_hour_overrides -- one-day allowed-hours window replacement

Revision ID: abfbdcbbb0e4
Revises: 7c1b9e0a2d34
Create Date: 2026-09-19 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "abfbdcbbb0e4"
down_revision: str | Sequence[str] | None = "7c1b9e0a2d34"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Unlike day_overrides (an absolute seconds replacement, hub-only), this
    # table's contents get pushed to the device -- see
    # timekpr_hub_core.effective_policy's module docstring. intervals_json
    # stores the resolved AllowedHourInterval list rather than a separate
    # mode flag; api/ui.py::_classify_day_hours already infers all/between/
    # custom back from stored intervals for display.
    op.create_table(
        "day_hour_overrides",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("intervals_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("jsonb_array_length(intervals_json) > 0", name="ck_day_hour_overrides_nonempty"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "day", name="uq_day_hour_overrides_user_day"),
    )
    op.create_index("ix_day_hour_overrides_user_day", "day_hour_overrides", ["user_id", "day"])


def downgrade() -> None:
    op.drop_index("ix_day_hour_overrides_user_day", table_name="day_hour_overrides")
    op.drop_table("day_hour_overrides")
