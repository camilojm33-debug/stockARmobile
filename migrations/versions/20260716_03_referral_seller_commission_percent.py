"""add seller-level referral commission percent

Revision ID: 20260716_03_referral_seller_commission_percent
Revises: 20260716_02_sales_client_txn_id
Create Date: 2026-07-16 02:10:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260716_03_referral_seller_commission_percent"
down_revision = "20260716_02_sales_client_txn_id"
branch_labels = None
depends_on = None


def _has_table(bind, table_name):
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def _has_column(bind, table_name, column_name):
    inspector = sa.inspect(bind)
    if table_name not in inspector.get_table_names():
        return False
    return column_name in {column["name"] for column in inspector.get_columns(table_name)}


def upgrade():
    bind = op.get_bind()

    if _has_table(bind, "referral_sellers") and not _has_column(bind, "referral_sellers", "commission_percent"):
        op.add_column(
            "referral_sellers",
            sa.Column("commission_percent", sa.Numeric(10, 4), nullable=False, server_default="0.3000"),
        )


def downgrade():
    bind = op.get_bind()

    if _has_table(bind, "referral_sellers") and _has_column(bind, "referral_sellers", "commission_percent"):
        op.drop_column("referral_sellers", "commission_percent")
