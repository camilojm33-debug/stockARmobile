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


def downgrade():
    op.alter_column(
        "quote_deliveries",
        "shipping_source",
        server_default="legacy_percent",
        existing_type=sa.String(length=30),
        existing_nullable=False,
    )
