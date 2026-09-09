"""Add tenant-scoped AI marketing campaigns."""

from alembic import op
import sqlalchemy as sa


revision = "20260903_01_ai_campaigns"
down_revision = "20260825_02_tenant_dependency_cascades"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "ai_campaigns",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=180), nullable=False),
        sa.Column("objective", sa.String(length=120), nullable=False),
        sa.Column("campaign_type", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="BORRADOR"),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("system_data_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("audience_segment", sa.String(length=120), nullable=True),
        sa.Column("audience_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("product_id", sa.Integer(), nullable=True),
        sa.Column("created_by_user_id", sa.Integer(), nullable=False),
        sa.Column("approved_by_user_id", sa.Integer(), nullable=True),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"]),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["approved_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ai_campaigns_company_status", "ai_campaigns", ["company_id", "status"])
    op.create_index("ix_ai_campaigns_company_created", "ai_campaigns", ["company_id", "created_at"])
    op.create_index("ix_ai_campaigns_company_id", "ai_campaigns", ["company_id"])
    op.create_index("ix_ai_campaigns_status", "ai_campaigns", ["status"])
    op.create_index("ix_ai_campaigns_created_at", "ai_campaigns", ["created_at"])
    op.create_index("ix_ai_campaigns_product_id", "ai_campaigns", ["product_id"])
    op.create_index("ix_ai_campaigns_created_by_user_id", "ai_campaigns", ["created_by_user_id"])
    op.create_index("ix_ai_campaigns_approved_by_user_id", "ai_campaigns", ["approved_by_user_id"])


def downgrade():
    op.drop_table("ai_campaigns")