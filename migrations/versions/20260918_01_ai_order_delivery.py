"""Add delivery data to AI orders and client location fields.

Revision ID: 20260918_01_ai_order_delivery
Revises: 20260916_01_ai_business_memory
"""
from alembic import op
import sqlalchemy as sa


revision = "20260918_01_ai_order_delivery"
down_revision = "20260916_01_ai_business_memory"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("clients", sa.Column("province", sa.String(length=120), nullable=True))
    op.add_column("clients", sa.Column("postal_code", sa.String(length=20), nullable=True))

    op.create_table(
        "quote_deliveries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("quote_id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("method", sa.String(length=20), nullable=False, server_default="retiro"),
        sa.Column("recipient_name", sa.String(length=160), nullable=True),
        sa.Column("phone", sa.String(length=40), nullable=True),
        sa.Column("address", sa.Text(), nullable=True),
        sa.Column("city", sa.String(length=100), nullable=True),
        sa.Column("province", sa.String(length=120), nullable=True),
        sa.Column("postal_code", sa.String(length=20), nullable=True),
        sa.Column("reference", sa.String(length=255), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("shipping_cost", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("shipping_rate", sa.Numeric(10, 4), nullable=False, server_default="0"),
        sa.Column("shipping_reason", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.ForeignKeyConstraint(["quote_id"], ["quotes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("quote_id", name="uq_quote_deliveries_quote_id"),
    )
    op.create_index("ix_quote_deliveries_quote", "quote_deliveries", ["quote_id"], unique=True)
    op.create_index("ix_quote_deliveries_company", "quote_deliveries", ["company_id"], unique=False)


def downgrade():
    op.drop_index("ix_quote_deliveries_company", table_name="quote_deliveries")
    op.drop_index("ix_quote_deliveries_quote", table_name="quote_deliveries")
    op.drop_table("quote_deliveries")
    op.drop_column("clients", "postal_code")
    op.drop_column("clients", "province")
