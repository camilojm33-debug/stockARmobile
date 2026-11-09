"""Add idempotency keys to branch stock transfers.

Revision ID: 20261109_02_branch_transfer_idempotency
Revises: 20261109_01_branch_receipts
"""
from alembic import op
import sqlalchemy as sa

revision = "20261109_02_branch_transfer_idempotency"
down_revision = "20261109_01_branch_receipts"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "branch_stock_transfers",
        sa.Column("idempotency_key", sa.String(length=100), nullable=True),
    )
    op.create_index(
        "uq_branch_stock_transfers_company_key_product",
        "branch_stock_transfers",
        ["company_id", "idempotency_key", "product_id"],
        unique=True,
    )


def downgrade():
    op.drop_index(
        "uq_branch_stock_transfers_company_key_product",
        table_name="branch_stock_transfers",
    )
    op.drop_column("branch_stock_transfers", "idempotency_key")
