"""Persist quote charge breakdown on sales for lossless conversion.

Revision ID: 20261008_01_quote_sale_flow_consistency
Revises: 20261007_01_public_vendor_idempotency
"""

from alembic import op
import sqlalchemy as sa


revision = "20261008_01_quote_sale_flow_consistency"
down_revision = "20261007_01_public_vendor_idempotency"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("sales", sa.Column("charges_json", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("sales", "charges_json")
