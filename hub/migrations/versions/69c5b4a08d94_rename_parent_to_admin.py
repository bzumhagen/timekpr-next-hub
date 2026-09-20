"""rename parent to admin

Revision ID: 69c5b4a08d94
Revises: c42766a96319
Create Date: 2026-09-20 02:10:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "69c5b4a08d94"
down_revision: str | Sequence[str] | None = "c42766a96319"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Table + FK column renames -- ALTER TABLE ... RENAME is a metadata-only
    # change in Postgres (no rewrite, no lock beyond ACCESS EXCLUSIVE for the
    # instant it takes), so this preserves every row and every existing FK,
    # unlike the drop-and-recreate autogenerate would otherwise produce.
    op.rename_table("parents", "admins")
    op.rename_table("parent_sessions", "admin_sessions")
    op.alter_column("admin_sessions", "parent_id", new_column_name="admin_id")
    op.rename_table("parent_invites", "admin_invites")
    op.alter_column("admin_invites", "created_by_parent_id", new_column_name="created_by_admin_id")

    # Constraint/index/sequence names inherited from the old table names --
    # Postgres doesn't rename these along with RENAME TABLE, and leaving
    # them mismatched is purely cosmetic (psql \d output, pg_dump diffs)
    # but confusing enough to fix while the whole vocabulary is changing.
    op.execute("ALTER INDEX parents_pkey RENAME TO admins_pkey")
    op.execute("ALTER TABLE admins RENAME CONSTRAINT parents_email_key TO admins_email_key")
    op.execute("ALTER INDEX parent_sessions_pkey RENAME TO admin_sessions_pkey")
    op.execute(
        "ALTER TABLE admin_sessions RENAME CONSTRAINT parent_sessions_token_hash_key "
        "TO admin_sessions_token_hash_key"
    )
    op.execute(
        "ALTER TABLE admin_sessions RENAME CONSTRAINT parent_sessions_parent_id_fkey "
        "TO admin_sessions_admin_id_fkey"
    )
    op.execute("ALTER TABLE admin_invites RENAME CONSTRAINT parent_invites_pkey TO admin_invites_pkey")
    op.execute(
        "ALTER TABLE admin_invites RENAME CONSTRAINT parent_invites_created_by_parent_id_fkey "
        "TO admin_invites_created_by_admin_id_fkey"
    )

    # actor_type/action vocabulary in the (append-only) audit log -- without
    # this the audit viewer would show a mix of 'parent'/'parent.*' rows
    # from before this release and 'admin'/'admin.*' after it, for no
    # reason a reader could tell apart.
    op.execute("UPDATE audit_log SET actor_type = 'admin' WHERE actor_type = 'parent'")
    op.execute("UPDATE audit_log SET target_type = 'admin' WHERE target_type = 'parent'")
    op.execute(
        "UPDATE audit_log SET action = 'admin.' || substring(action from 8) WHERE action LIKE 'parent.%'"
    )

    # Grant.source: the same 'who granted this' vocabulary, in a CHECK
    # constraint rather than free text -- the constraint itself, not just
    # the data, has to move or every new admin-sourced grant 500s.
    op.drop_constraint("ck_grants_source", "grants", type_="check")
    op.execute("UPDATE grants SET source = 'admin' WHERE source = 'parent'")
    op.create_check_constraint("ck_grants_source", "grants", "source IN ('admin')")


def downgrade() -> None:
    op.drop_constraint("ck_grants_source", "grants", type_="check")
    op.execute("UPDATE grants SET source = 'parent' WHERE source = 'admin'")
    op.create_check_constraint("ck_grants_source", "grants", "source IN ('parent')")

    op.execute(
        "UPDATE audit_log SET action = 'parent.' || substring(action from 7) WHERE action LIKE 'admin.%'"
    )
    op.execute("UPDATE audit_log SET target_type = 'parent' WHERE target_type = 'admin'")
    op.execute("UPDATE audit_log SET actor_type = 'parent' WHERE actor_type = 'admin'")

    op.execute(
        "ALTER TABLE admin_invites RENAME CONSTRAINT admin_invites_created_by_admin_id_fkey "
        "TO parent_invites_created_by_parent_id_fkey"
    )
    op.execute("ALTER TABLE admin_invites RENAME CONSTRAINT admin_invites_pkey TO parent_invites_pkey")
    op.execute(
        "ALTER TABLE admin_sessions RENAME CONSTRAINT admin_sessions_admin_id_fkey "
        "TO parent_sessions_parent_id_fkey"
    )
    op.execute(
        "ALTER TABLE admin_sessions RENAME CONSTRAINT admin_sessions_token_hash_key "
        "TO parent_sessions_token_hash_key"
    )
    op.execute("ALTER INDEX admin_sessions_pkey RENAME TO parent_sessions_pkey")
    op.execute("ALTER TABLE admins RENAME CONSTRAINT admins_email_key TO parents_email_key")
    op.execute("ALTER INDEX admins_pkey RENAME TO parents_pkey")

    op.alter_column("admin_invites", "created_by_admin_id", new_column_name="created_by_parent_id")
    op.rename_table("admin_invites", "parent_invites")
    op.alter_column("admin_sessions", "admin_id", new_column_name="parent_id")
    op.rename_table("admin_sessions", "parent_sessions")
    op.rename_table("admins", "parents")
