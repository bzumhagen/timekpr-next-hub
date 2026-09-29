"""user_aliases.policy_push_error -- surface a stuck setter on the hub

Revision ID: b4d1e7a90c32
Revises: 69c5b4a08d94
Create Date: 2026-09-28 22:10:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b4d1e7a90c32"
down_revision: str | Sequence[str] | None = "69c5b4a08d94"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Diagnostic only -- /sync writes whatever the agent reports and nothing
    # reads these to make a decision. Both nullable with no backfill: NULL
    # is exactly the correct value for every existing row (no failure
    # reported), and agents that predate SyncUserRequest.policy_push_error
    # simply never populate them.
    op.add_column("user_aliases", sa.Column("policy_push_error", sa.String(length=256), nullable=True))
    op.add_column(
        "user_aliases", sa.Column("policy_push_error_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("user_aliases", "policy_push_error_at")
    op.drop_column("user_aliases", "policy_push_error")
