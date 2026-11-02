"""Add per-company memberships for a single login identity.

Existing users are backfilled as members of their current company. Their
company_id remains as a legacy/default-company pointer for compatibility.
"""
from alembic import op
import sqlalchemy as sa

revision = "20261102_02_company_memberships"
down_revision = "8a0e95fc2189"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "user_company_memberships",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False, server_default="user"),
        sa.Column("permissions_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.UniqueConstraint("user_id", "company_id", name="uq_user_company_membership"),
    )
    op.create_index(
        "ix_user_company_memberships_company_active",
        "user_company_memberships",
        ["company_id", "active"],
    )
    op.create_index(
        "ix_user_company_memberships_user_active",
        "user_company_memberships",
        ["user_id", "active"],
    )
    bind = op.get_bind()
    bind.execute(sa.text(
        "INSERT INTO user_company_memberships "
        "(user_id, company_id, role, permissions_json, active, created_at, updated_at) "
        "SELECT u.id, u.company_id, COALESCE(NULLIF(u.role, ''), 'user'), "
        "COALESCE(u.permissions_json, '[]'), u.active, "
        "COALESCE(u.created_at, CURRENT_TIMESTAMP), CURRENT_TIMESTAMP "
        "FROM users u WHERE u.company_id IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM user_company_memberships m "
        "WHERE m.user_id=u.id AND m.company_id=u.company_id)"
    ))


def downgrade():
    op.drop_index("ix_user_company_memberships_user_active", table_name="user_company_memberships")
    op.drop_index("ix_user_company_memberships_company_active", table_name="user_company_memberships")
    op.drop_table("user_company_memberships")
