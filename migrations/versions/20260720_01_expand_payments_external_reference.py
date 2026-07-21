"""expand payments external_reference length

Revision ID: 20260720_01_expand_payments_external_reference
Revises: 20260716_04_sales_comprobante_fields
Create Date: 2026-07-20 22:40:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260720_01_expand_payments_external_reference"
down_revision = "20260716_04_sales_comprobante_fields"
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
    if _has_table(bind, "payments") and _has_column(bind, "payments", "external_reference"):
        op.alter_column(
            "payments",
            "external_reference",
            existing_type=sa.String(length=120),
            type_=sa.String(length=255),
            existing_nullable=True,
        )


def downgrade():
    bind = op.get_bind()
    if _has_table(bind, "payments") and _has_column(bind, "payments", "external_reference"):
        op.alter_column(
            "payments",
            "external_reference",
            existing_type=sa.String(length=255),
            type_=sa.String(length=120),
            existing_nullable=True,
        )
