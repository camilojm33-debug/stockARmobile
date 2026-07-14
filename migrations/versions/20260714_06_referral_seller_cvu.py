"""Add CVU field to referral sellers.

Revision ID: 20260714_06_referral_seller_cvu
Revises: 20260714_05_referral_payout_payment_method
Create Date: 2026-07-14
"""

from alembic import op
import sqlalchemy as sa


revision = "20260714_06_referral_seller_cvu"
down_revision = "20260714_05_referral_payout_payment_method"
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


def upgrade() -> None:
    bind = op.get_bind()
    if _has_table(bind, "referral_sellers") and not _has_column(bind, "referral_sellers", "cvu"):
        op.add_column("referral_sellers", sa.Column("cvu", sa.String(length=30), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if _has_table(bind, "referral_sellers") and _has_column(bind, "referral_sellers", "cvu"):
        op.drop_column("referral_sellers", "cvu")
