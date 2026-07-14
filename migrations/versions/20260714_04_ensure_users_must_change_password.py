"""Ensure users.must_change_password exists with safe default.

Revision ID: 20260714_04_ensure_users_must_change_password
Revises: 20260714_03_referral_commissions_note
Create Date: 2026-07-14
"""

from alembic import op
import sqlalchemy as sa


revision = "20260714_04_ensure_users_must_change_password"
down_revision = "20260714_03_referral_commissions_note"
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
    if _has_table(bind, "users") and not _has_column(bind, "users", "must_change_password"):
        op.add_column("users", sa.Column("must_change_password", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    bind = op.get_bind()
    if _has_table(bind, "users") and _has_column(bind, "users", "must_change_password"):
        op.drop_column("users", "must_change_password")
