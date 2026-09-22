"""Create tenant-scoped commercial promotions.
Revision ID: 20260922_03_promotions
Revises: 20260922_02_vendor_fixed_shipping
"""
from alembic import op
import sqlalchemy as sa

revision = "20260922_03_promotions"
down_revision = "20260922_02_vendor_fixed_shipping"
branch_labels = None
depends_on = None

def upgrade():
    op.create_table(
        "promotions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=True),
        sa.Column("category", sa.String(length=100), nullable=True),
        sa.Column("name", sa.String(length=180), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("type", sa.String(length=30), nullable=False),
        sa.Column("min_quantity", sa.Numeric(18, 3), nullable=True),
        sa.Column("buy_quantity", sa.Numeric(18, 3), nullable=True),
        sa.Column("pay_quantity", sa.Numeric(18, 3), nullable=True),
        sa.Column("discount_percent", sa.Numeric(10, 4), nullable=True),
        sa.Column("discount_amount", sa.Numeric(18, 2), nullable=True),
        sa.Column("starts_at", sa.DateTime(), nullable=True),
        sa.Column("ends_at", sa.DateTime(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="ACTIVA"),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("stackable", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_promotions_company_active_dates", "promotions", ["company_id", "active", "starts_at", "ends_at"])
    op.create_index("ix_promotions_company_product", "promotions", ["company_id", "product_id"])

def downgrade():
    op.drop_index("ix_promotions_company_product", table_name="promotions")
    op.drop_index("ix_promotions_company_active_dates", table_name="promotions")
    op.drop_table("promotions")
