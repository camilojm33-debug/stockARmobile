"""add comprobante request fields to sales

Revision ID: 20260716_04_sales_comprobante_fields
Revises: 20260716_03_referral_seller_commission_percent
Create Date: 2026-07-16 04:10:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260716_04_sales_comprobante_fields"
down_revision = "20260716_03_referral_seller_commission_percent"
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


def _has_index(bind, table_name, index_name):
    inspector = sa.inspect(bind)
    if table_name not in inspector.get_table_names():
        return False
    return index_name in {index["name"] for index in inspector.get_indexes(table_name)}


def upgrade():
    bind = op.get_bind()

    if _has_table(bind, "sales") and not _has_column(bind, "sales", "requiere_comprobante"):
        op.add_column("sales", sa.Column("requiere_comprobante", sa.Boolean(), nullable=False, server_default=sa.false()))

    if _has_table(bind, "sales") and not _has_column(bind, "sales", "tipo_comprobante"):
        op.add_column("sales", sa.Column("tipo_comprobante", sa.String(length=30), nullable=True))

    if _has_table(bind, "sales") and not _has_column(bind, "sales", "observacion_comprobante"):
        op.add_column("sales", sa.Column("observacion_comprobante", sa.String(length=255), nullable=True))

    if _has_table(bind, "sales") and not _has_column(bind, "sales", "comprobante_emitido"):
        op.add_column("sales", sa.Column("comprobante_emitido", sa.Boolean(), nullable=False, server_default=sa.false()))

    if _has_table(bind, "sales") and not _has_index(bind, "sales", "ix_sales_requiere_comprobante"):
        op.create_index("ix_sales_requiere_comprobante", "sales", ["requiere_comprobante"], unique=False)

    if _has_table(bind, "sales") and not _has_index(bind, "sales", "ix_sales_comprobante_emitido"):
        op.create_index("ix_sales_comprobante_emitido", "sales", ["comprobante_emitido"], unique=False)


def downgrade():
    bind = op.get_bind()

    if _has_table(bind, "sales") and _has_index(bind, "sales", "ix_sales_comprobante_emitido"):
        op.drop_index("ix_sales_comprobante_emitido", table_name="sales")

    if _has_table(bind, "sales") and _has_index(bind, "sales", "ix_sales_requiere_comprobante"):
        op.drop_index("ix_sales_requiere_comprobante", table_name="sales")

    if _has_table(bind, "sales") and _has_column(bind, "sales", "comprobante_emitido"):
        op.drop_column("sales", "comprobante_emitido")

    if _has_table(bind, "sales") and _has_column(bind, "sales", "observacion_comprobante"):
        op.drop_column("sales", "observacion_comprobante")

    if _has_table(bind, "sales") and _has_column(bind, "sales", "tipo_comprobante"):
        op.drop_column("sales", "tipo_comprobante")

    if _has_table(bind, "sales") and _has_column(bind, "sales", "requiere_comprobante"):
        op.drop_column("sales", "requiere_comprobante")
