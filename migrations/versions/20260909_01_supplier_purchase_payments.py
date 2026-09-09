"""supplier purchase payment details

Revision ID: 20260909_01_supplier_purchase_payments
Revises: 20260908_01
Create Date: 2026-09-09
"""

from alembic import op
import sqlalchemy as sa


revision = "20260909_01_supplier_purchase_payments"
down_revision = "20260908_01"
branch_labels = None
depends_on = None


MONEY = sa.Numeric(18, 2)


def upgrade():
    op.create_table(
        "purchase_payment_details",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("purchase_order_id", sa.Integer(), sa.ForeignKey("purchase_orders.id", ondelete="CASCADE"), nullable=False, unique=True, index=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("payment_method", sa.String(40), nullable=False, server_default="CUENTA_CORRIENTE"),
        sa.Column("paid_amount", MONEY, nullable=False, server_default="0.00"),
        sa.Column("secondary_payment_method", sa.String(40), nullable=True),
        sa.Column("secondary_paid_amount", MONEY, nullable=False, server_default="0.00"),
        sa.Column("payment_term_days", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("due_date", sa.Date(), nullable=True, index=True),
        sa.Column("payment_status", sa.String(20), nullable=False, server_default="pendiente", index=True),
        sa.Column("payment_reference", sa.String(120), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("paid_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )


def downgrade():
    op.drop_table("purchase_payment_details")
