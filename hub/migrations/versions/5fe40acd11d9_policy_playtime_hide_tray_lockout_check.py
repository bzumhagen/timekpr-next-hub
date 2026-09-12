"""policy: playtime, hide_tray_icon, lockout check

Revision ID: 5fe40acd11d9
Revises: 3f9a1c7d4e21
Create Date: 2026-09-12 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "5fe40acd11d9"
down_revision: str | Sequence[str] | None = "3f9a1c7d4e21"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "policies", sa.Column("hide_tray_icon", sa.Boolean(), server_default="false", nullable=False)
    )
    op.add_column(
        "policies", sa.Column("playtime_enabled", sa.Boolean(), server_default="false", nullable=False)
    )
    op.add_column(
        "policies",
        sa.Column("playtime_override_enabled", sa.Boolean(), server_default="false", nullable=False),
    )
    op.add_column(
        "policies",
        sa.Column(
            "playtime_unaccounted_intervals_enabled", sa.Boolean(), server_default="true", nullable=False
        ),
    )
    op.add_column(
        "policies",
        sa.Column(
            "playtime_allowed_weekdays_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default='["1", "2", "3", "4", "5", "6", "7"]',
            nullable=False,
        ),
    )
    op.add_column(
        "policies",
        sa.Column(
            "playtime_daily_limits_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[0, 0, 0, 0, 0, 0, 0]",
            nullable=False,
        ),
    )
    op.add_column(
        "policies",
        sa.Column(
            "playtime_activities_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_policies_lockout_type",
        "policies",
        "lockout_type IN ('lock', 'suspend', 'suspendwake', 'terminate', 'kill', 'shutdown')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_policies_lockout_type", "policies", type_="check")
    op.drop_column("policies", "playtime_activities_json")
    op.drop_column("policies", "playtime_daily_limits_json")
    op.drop_column("policies", "playtime_allowed_weekdays_json")
    op.drop_column("policies", "playtime_unaccounted_intervals_enabled")
    op.drop_column("policies", "playtime_override_enabled")
    op.drop_column("policies", "playtime_enabled")
    op.drop_column("policies", "hide_tray_icon")
