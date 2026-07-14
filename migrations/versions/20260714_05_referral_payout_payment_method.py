"""Add payment method to referral payouts.

Revision ID: 20260714_05_referral_payout_payment_method
Revises: 20260714_04_ensure_users_must_change_password
Create Date: 2026-07-14
"""

from alembic import op
import sqlalchemy as sa


revision = "20260714_05_referral_payout_payment_method"
down_revision = "20260714_04_ensure_users_must_change_password"
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
    if _has_table(bind, "referral_payouts") and not _has_column(bind, "referral_payouts", "payment_method"):
        op.add_column("referral_payouts", sa.Column("payment_method", sa.String(length=80), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if _has_table(bind, "referral_payouts") and _has_column(bind, "referral_payouts", "payment_method"):
        op.drop_column("referral_payouts", "payment_method")
