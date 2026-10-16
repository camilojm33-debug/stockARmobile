"""Store structured reason when a tenant CRM opportunity is lost.

Revision ID: 20261016_01_crm_loss_reason
Revises: 20261008_01_quote_sale_flow_consistency
Create Date: 2026-10-16
"""
from alembic import op
import sqlalchemy as sa


revision = "20261016_01_crm_loss_reason"
down_revision = "20261008_01_quote_sale_flow_consistency"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("crm_opportunities", sa.Column("loss_reason", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("crm_opportunities", "loss_reason")
