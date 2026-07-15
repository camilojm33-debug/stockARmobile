"""Add location and contact fields to companies.

Revision ID: 20260714_08_company_location_contact_fields
Revises: 20260714_07_resource_messages
Create Date: 2026-07-14
"""

from alembic import op
import sqlalchemy as sa


revision = "20260714_08_company_location_contact_fields"
down_revision = "20260714_07_resource_messages"
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
    if not _has_table(bind, "companies"):
        return

    if not _has_column(bind, "companies", "province"):
        op.add_column("companies", sa.Column("province", sa.String(length=120), nullable=True))
    if not _has_column(bind, "companies", "city"):
        op.add_column("companies", sa.Column("city", sa.String(length=120), nullable=True))
    if not _has_column(bind, "companies", "postal_code"):
        op.add_column("companies", sa.Column("postal_code", sa.String(length=20), nullable=True))
    if not _has_column(bind, "companies", "whatsapp"):
        op.add_column("companies", sa.Column("whatsapp", sa.String(length=40), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if not _has_table(bind, "companies"):
        return

    if _has_column(bind, "companies", "whatsapp"):
        op.drop_column("companies", "whatsapp")
    if _has_column(bind, "companies", "postal_code"):
        op.drop_column("companies", "postal_code")
    if _has_column(bind, "companies", "city"):
        op.drop_column("companies", "city")
    if _has_column(bind, "companies", "province"):
        op.drop_column("companies", "province")
