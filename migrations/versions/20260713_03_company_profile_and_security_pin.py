"""Add business profile and secure PIN fields for company settings.

Revision ID: 20260713_03
Revises: 20260712_02
Create Date: 2026-07-13
"""

from alembic import op
import sqlalchemy as sa


revision = "20260713_03"
down_revision = "20260712_02"
branch_labels = None
depends_on = None


def _has_column(bind, table_name, column_name):
    inspector = sa.inspect(bind)
    if table_name not in inspector.get_table_names():
        return False
    return column_name in {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    bind = op.get_bind()

    if not _has_column(bind, "companies", "legal_name"):
        op.add_column("companies", sa.Column("legal_name", sa.String(length=160), nullable=True))
    if not _has_column(bind, "companies", "address"):
        op.add_column("companies", sa.Column("address", sa.String(length=255), nullable=True))
    if not _has_column(bind, "companies", "phone"):
        op.add_column("companies", sa.Column("phone", sa.String(length=40), nullable=True))
    if not _has_column(bind, "companies", "business_pin_hash"):
        op.add_column("companies", sa.Column("business_pin_hash", sa.String(length=255), nullable=True))
    if not _has_column(bind, "companies", "business_pin_failed_attempts"):
        op.add_column("companies", sa.Column("business_pin_failed_attempts", sa.Integer(), nullable=False, server_default="0"))
    if not _has_column(bind, "companies", "business_pin_blocked_until"):
        op.add_column("companies", sa.Column("business_pin_blocked_until", sa.DateTime(), nullable=True))
    if not _has_column(bind, "companies", "business_pin_updated_at"):
        op.add_column("companies", sa.Column("business_pin_updated_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()

    if _has_column(bind, "companies", "business_pin_updated_at"):
        op.drop_column("companies", "business_pin_updated_at")
    if _has_column(bind, "companies", "business_pin_blocked_until"):
        op.drop_column("companies", "business_pin_blocked_until")
    if _has_column(bind, "companies", "business_pin_failed_attempts"):
        op.drop_column("companies", "business_pin_failed_attempts")
    if _has_column(bind, "companies", "business_pin_hash"):
        op.drop_column("companies", "business_pin_hash")
    if _has_column(bind, "companies", "phone"):
        op.drop_column("companies", "phone")
    if _has_column(bind, "companies", "address"):
        op.drop_column("companies", "address")
    if _has_column(bind, "companies", "legal_name"):
        op.drop_column("companies", "legal_name")
