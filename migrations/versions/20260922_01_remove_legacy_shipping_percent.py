"""Retire the legacy percentage-shipping database default.

Revision ID: 20260922_01_remove_legacy_shipping_percent
Revises: 20260921_01_ai_shipping_workflow
"""
from alembic import op
import sqlalchemy as sa

revision = "20260922_01_remove_legacy_shipping_percent"
down_revision = "20260921_01_ai_shipping_workflow"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column(
        "quote_deliveries",
        "shipping_source",
        server_default="manual",
        existing_type=sa.String(length=30),
        existing_nullable=False,
    )
    op.execute(
        sa.text(
            "UPDATE agent_configurations SET max_tokens = 1200 "
            "WHERE max_tokens = 700 AND agent_id IN "
            "(SELECT id FROM agents WHERE name = 'Vendedor 24 hs')"
        )
    )


def downgrade():
    op.execute(
        sa.text(
            "UPDATE agent_configurations SET max_tokens = 700 "
            "WHERE max_tokens = 1200 AND agent_id IN "
            "(SELECT id FROM agents WHERE name = 'Vendedor 24 hs')"
        )
    )
    op.alter_column(
        "quote_deliveries",
        "shipping_source",
        server_default="legacy_percent",
        existing_type=sa.String(length=30),
        existing_nullable=False,
    )
