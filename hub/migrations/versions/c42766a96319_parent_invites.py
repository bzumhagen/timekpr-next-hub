"""parent invites for a second admin account

Revision ID: c42766a96319
Revises: a8a6c9dd2e83
Create Date: 2026-09-20 01:39:44.640165

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c42766a96319"
down_revision: str | Sequence[str] | None = "a8a6c9dd2e83"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Single-use, expiring invite for a second (or third, ...) parent
    # account -- the EnrollmentCode pattern applied to parents instead of
    # devices. created_by_parent_id is SET NULL (not CASCADE) so deleting
    # the inviting parent later doesn't erase the invite's own history.
    op.create_table(
        "parent_invites",
        sa.Column("token", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_parent_id", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["created_by_parent_id"], ["parents.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("token"),
    )


def downgrade() -> None:
    op.drop_table("parent_invites")
