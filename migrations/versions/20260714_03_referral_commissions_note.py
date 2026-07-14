"""Add note column to referral commissions.

Revision ID: 20260714_03_referral_commissions_note
Revises: 20260714_02_landing_testimonials
Create Date: 2026-07-13
"""

from alembic import op
import sqlalchemy as sa


revision = "20260714_03_referral_commissions_note"
down_revision = "20260714_02_landing_testimonials"
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

    if bind.dialect.name == "postgresql":
        op.execute("ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(64)")

    if _has_table(bind, "referral_commissions") and not _has_column(bind, "referral_commissions", "note"):
        op.add_column("referral_commissions", sa.Column("note", sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if _has_table(bind, "referral_commissions") and _has_column(bind, "referral_commissions", "note"):
        op.drop_column("referral_commissions", "note")
