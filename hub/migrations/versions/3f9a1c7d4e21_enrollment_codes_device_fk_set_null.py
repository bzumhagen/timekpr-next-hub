"""enrollment_codes.used_by_device_id FK: RESTRICT -> SET NULL on delete

Revision ID: 3f9a1c7d4e21
Revises: 8f3c2a1e9b04
Create Date: 2026-09-12 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3f9a1c7d4e21"
down_revision: str | Sequence[str] | None = "8f3c2a1e9b04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "enrollment_codes_used_by_device_id_fkey"


def upgrade() -> None:
    # The initial schema left this FK with no ondelete action (defaulting
    # to RESTRICT), which meant deleting ANY device 500'd with a foreign-
    # key violation -- every enrolled device has an enrollment_codes row
    # pointing at it via used_by_device_id. SET NULL keeps the historical
    # "this code was redeemed, and when" record intact; it just stops
    # pointing at a device that no longer exists (see api/ui/devices.py's
    # delete_device_ui / api/admin/devices.py's delete_device -- the DELETE
    # /devices/{id} action added alongside revoke).
    op.drop_constraint(_CONSTRAINT, "enrollment_codes", type_="foreignkey")
    op.create_foreign_key(
        _CONSTRAINT, "enrollment_codes", "devices", ["used_by_device_id"], ["id"], ondelete="SET NULL"
    )


def downgrade() -> None:
    op.drop_constraint(_CONSTRAINT, "enrollment_codes", type_="foreignkey")
    op.create_foreign_key(_CONSTRAINT, "enrollment_codes", "devices", ["used_by_device_id"], ["id"])
