"""Add explicit shipping workflow state to AI orders.

Revision ID: 20260921_01_ai_shipping_workflow
Revises: 20260918_02_company_logo_persistence
"""
from alembic import op
import sqlalchemy as sa


revision = "20260921_01_ai_shipping_workflow"
down_revision = "20260918_02_company_logo_persistence"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "quote_deliveries",
        sa.Column("shipping_status", sa.String(length=30), nullable=False, server_default="confirmed"),
    )
    op.add_column(
        "quote_deliveries",
        sa.Column("shipping_source", sa.String(length=30), nullable=False, server_default="legacy_percent"),
    )
    op.add_column(
        "quote_deliveries",
        sa.Column("shipping_confirmed_by_user_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "quote_deliveries",
        sa.Column("shipping_confirmed_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_quote_deliveries_shipping_status",
        "quote_deliveries",
        ["shipping_status"],
        unique=False,
    )
    op.create_foreign_key(
        "fk_quote_deliveries_shipping_confirmed_by_user",
        "quote_deliveries",
        "users",
        ["shipping_confirmed_by_user_id"],
        ["id"],
    )


def downgrade():
    op.drop_constraint(
        "fk_quote_deliveries_shipping_confirmed_by_user",
        "quote_deliveries",
        type_="foreignkey",
    )
    op.drop_index("ix_quote_deliveries_shipping_status", table_name="quote_deliveries")
    op.drop_column("quote_deliveries", "shipping_confirmed_at")
    op.drop_column("quote_deliveries", "shipping_confirmed_by_user_id")
    op.drop_column("quote_deliveries", "shipping_source")
    op.drop_column("quote_deliveries", "shipping_status")
