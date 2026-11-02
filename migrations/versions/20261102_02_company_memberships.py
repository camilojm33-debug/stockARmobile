"""Add per-company memberships for shared user identities.

Revision ID: 20261102_02_company_memberships
Revises: 20261102_01_multibranch_opening
"""
from alembic import op
import sqlalchemy as sa

revision = "20261102_02_company_memberships"
down_revision = "20261102_01_multibranch_opening"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "company_memberships",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False, server_default="user"),
        sa.Column("permissions_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column("invitation_token_hash", sa.String(length=64), nullable=True),
        sa.Column("invitation_expires_at", sa.DateTime(), nullable=True),
        sa.Column("invited_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("accepted_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.UniqueConstraint("user_id", "company_id", name="uq_company_memberships_user_company"),
    )
    op.create_index("ix_company_memberships_user", "company_memberships", ["user_id"])
    op.create_index("ix_company_memberships_company", "company_memberships", ["company_id"])
    op.create_index("ix_company_memberships_status", "company_memberships", ["company_id", "status", "active"])
    op.create_index("ix_company_memberships_invitation_hash", "company_memberships", ["invitation_token_hash"], unique=True)

    bind = op.get_bind()
    legacy_users = bind.execute(
        sa.text(
            "SELECT id, company_id, role, permissions_json, active "
            "FROM users WHERE company_id IS NOT NULL ORDER BY id"
        )
    ).mappings().all()
    for row in legacy_users:
        bind.execute(
            sa.text(
                "INSERT INTO company_memberships "
                "(user_id, company_id, role, permissions_json, active, status, created_at) "
                "VALUES (:user_id, :company_id, :role, :permissions_json, :active, :status, CURRENT_TIMESTAMP)"
            ),
            {
                "user_id": row["id"],
                "company_id": row["company_id"],
                "role": row["role"] or "user",
                "permissions_json": row["permissions_json"] or "[]",
                "active": bool(row["active"]),
                "status": "active" if row["active"] else "revoked",
            },
        )


def downgrade():
    op.drop_index("ix_company_memberships_invitation_hash", table_name="company_memberships")
    op.drop_index("ix_company_memberships_status", table_name="company_memberships")
    op.drop_index("ix_company_memberships_company", table_name="company_memberships")
    op.drop_index("ix_company_memberships_user", table_name="company_memberships")
    op.drop_table("company_memberships")
