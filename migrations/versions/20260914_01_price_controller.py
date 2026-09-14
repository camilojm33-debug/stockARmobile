"""Add tenant-scoped global price controller batches and rollback items."""
from alembic import op
import sqlalchemy as sa

revision = "20260914_01_price_controller"
down_revision = "20260910_02_referral_network_payouts"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "price_controller_batches",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("adjustment_type", sa.String(20), nullable=False),
        sa.Column("direction", sa.String(12), nullable=False),
        sa.Column("adjustment_value", sa.Numeric(18, 4), nullable=False),
        sa.Column("category", sa.String(100)),
        sa.Column("brand", sa.String(120)),
        sa.Column("supplier", sa.String(160)),
        sa.Column("only_in_stock", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("rounding", sa.String(20), nullable=False, server_default="none"),
        sa.Column("min_price", sa.Numeric(18, 2)),
        sa.Column("max_price", sa.Numeric(18, 2)),
        sa.Column("min_margin_percent", sa.Numeric(10, 4)),
        sa.Column("max_change_percent", sa.Numeric(10, 4)),
        sa.Column("status", sa.String(24), nullable=False, server_default="preview"),
        sa.Column("product_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("blocked_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_delta", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("applied_at", sa.DateTime()),
        sa.Column("rolled_back_at", sa.DateTime()),
    )
    op.create_index("ix_price_controller_batches_company_created", "price_controller_batches", ["company_id", "created_at"])
    op.create_index("ix_price_controller_batches_company_status", "price_controller_batches", ["company_id", "status"])

    op.create_table(
        "price_controller_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("batch_id", sa.Integer(), sa.ForeignKey("price_controller_batches.id", ondelete="CASCADE"), nullable=False),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("old_price", sa.Numeric(18, 2), nullable=False),
        sa.Column("new_price", sa.Numeric(18, 2), nullable=False),
        sa.Column("old_margin", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("new_margin", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("old_profit_percent", sa.Numeric(10, 4), nullable=False, server_default="0"),
        sa.Column("new_profit_percent", sa.Numeric(10, 4), nullable=False, server_default="0"),
        sa.Column("status", sa.String(24), nullable=False, server_default="ready"),
        sa.Column("reason", sa.String(255)),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("rolled_back_at", sa.DateTime()),
    )
    op.create_index("ix_price_controller_items_batch", "price_controller_items", ["batch_id"])
    op.create_index("ix_price_controller_items_product", "price_controller_items", ["product_id"])
    op.create_index("ix_price_controller_items_status", "price_controller_items", ["status"])


def downgrade():
    op.drop_index("ix_price_controller_items_status", table_name="price_controller_items")
    op.drop_index("ix_price_controller_items_product", table_name="price_controller_items")
    op.drop_index("ix_price_controller_items_batch", table_name="price_controller_items")
    op.drop_table("price_controller_items")
    op.drop_index("ix_price_controller_batches_company_status", table_name="price_controller_batches")
    op.drop_index("ix_price_controller_batches_company_created", table_name="price_controller_batches")
    op.drop_table("price_controller_batches")
