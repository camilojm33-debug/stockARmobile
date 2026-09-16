"""Add structured tenant-scoped AI business memory."""

from alembic import op
import sqlalchemy as sa

revision = "20260916_01_ai_business_memory"
down_revision = "20260914_01_price_controller"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "ai_business_memories",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("category", sa.String(length=60), nullable=False),
        sa.Column("memory_key", sa.String(length=120), nullable=False),
        sa.Column("value", sa.Text(), nullable=False, server_default=""),
        sa.Column("source", sa.String(length=40), nullable=False, server_default="manual"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("company_id", "category", "memory_key", name="uq_ai_business_memory_company_category_key"),
    )
    op.create_index("ix_ai_business_memories_company_id", "ai_business_memories", ["company_id"])
    op.create_index("ix_ai_business_memories_category", "ai_business_memories", ["category"])
    op.create_index("ix_ai_business_memories_active", "ai_business_memories", ["active"])
    op.create_index("ix_ai_business_memory_company_active", "ai_business_memories", ["company_id", "active"])


def downgrade():
    op.drop_index("ix_ai_business_memory_company_active", table_name="ai_business_memories")
    op.drop_index("ix_ai_business_memories_active", table_name="ai_business_memories")
    op.drop_index("ix_ai_business_memories_category", table_name="ai_business_memories")
    op.drop_index("ix_ai_business_memories_company_id", table_name="ai_business_memories")
    op.drop_table("ai_business_memories")
