"""activity_state column, missing (user_id, day) indexes, machine_id rebind

Revision ID: 8f3c2a1e9b04
Revises: fafdbdc4fd31
Create Date: 2026-09-11 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8f3c2a1e9b04"
down_revision: str | Sequence[str] | None = "fafdbdc4fd31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("usage_counters", sa.Column("activity_state", sa.String(length=16), nullable=True))

    op.create_index("ix_usage_counters_user_day", "usage_counters", ["user_id", "day"])
    op.create_index("ix_activity_intervals_user_day", "activity_intervals", ["user_id", "day"])
    op.create_index("ix_grants_user_day", "grants", ["user_id", "day"])

    # Reconcile pre-existing duplicates before the unique index below can be
    # created at all: any deployment that ran before enroll.py's rebind-on-
    # machine_id logic existed can have several non-revoked device rows for
    # the same physical machine (every reinstall/re-enroll used to create a
    # fresh one -- the exact bug this migration's index exists to prevent
    # going forward). Keep the most-recently-enrolled row per machine_id
    # live and revoke the rest; nothing is deleted, so each row's usage
    # history stays intact and visible, just no longer double-counted as if
    # two machines existed. A fresh install has no devices yet, so this is a
    # no-op there.
    op.execute(
        sa.text(
            """
            UPDATE devices d
            SET status = 'revoked'
            FROM (
                SELECT id, machine_id,
                       row_number() OVER (
                           PARTITION BY machine_id ORDER BY enrolled_at DESC, id DESC
                       ) AS rn
                FROM devices
                WHERE status <> 'revoked'
            ) ranked
            WHERE d.id = ranked.id AND ranked.rn > 1
            """
        )
    )

    # Partial unique index: a machine_id must be unique among non-revoked
    # devices, but a revoked device's machine_id shouldn't block a later,
    # genuinely new device row (or the same machine re-enrolling to a fresh
    # row after a parent explicitly revoked the old one) -- see
    # hub/timekpr_hub/api/enroll.py's rebind-on-machine_id lookup.
    op.create_index(
        "uq_devices_machine_id_live",
        "devices",
        ["machine_id"],
        unique=True,
        postgresql_where=sa.text("status <> 'revoked'"),
    )


def downgrade() -> None:
    op.drop_index("uq_devices_machine_id_live", table_name="devices")
    op.drop_index("ix_grants_user_day", table_name="grants")
    op.drop_index("ix_activity_intervals_user_day", table_name="activity_intervals")
    op.drop_index("ix_usage_counters_user_day", table_name="usage_counters")
    op.drop_column("usage_counters", "activity_state")
