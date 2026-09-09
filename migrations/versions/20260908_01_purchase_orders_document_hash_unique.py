"""Add source_document_hash to purchase_orders with tenant-scoped unique index.

Revision ID: 20260908_01
Revises: 20260903_01_ai_campaigns
Create Date: 2026-09-08
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260908_01"
down_revision = "20260903_01_ai_campaigns"
branch_labels = None
depends_on = None


def _has_table(bind, table_name):
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def _has_column(bind, table_name, column_name):
    inspector = sa.inspect(bind)
    if table_name not in inspector.get_table_names():
        return False
    return column_name in {col["name"] for col in inspector.get_columns(table_name)}


def _has_index(bind, table_name, index_name):
    inspector = sa.inspect(bind)
    if table_name not in inspector.get_table_names():
        return False
    return index_name in {index["name"] for index in inspector.get_indexes(table_name)}


def upgrade() -> None:
    bind = op.get_bind()

    if not _has_table(bind, "purchase_orders"):
        return

    if not _has_column(bind, "purchase_orders", "source_document_hash"):
        op.add_column("purchase_orders", sa.Column("source_document_hash", sa.String(length=64), nullable=True))

    # NULL no colisiona en un indice UNIQUE (SQLite y PostgreSQL tratan cada NULL como distinto),
    # por lo que las compras manuales (sin factura IA) conviven sin conflicto.
    if not _has_index(bind, "purchase_orders", "ix_purchase_orders_company_document_hash"):
        op.create_index(
            "ix_purchase_orders_company_document_hash",
            "purchase_orders",
            ["company_id", "source_document_hash"],
            unique=True,
        )


def downgrade() -> None:
    bind = op.get_bind()

    if _has_table(bind, "purchase_orders") and _has_index(bind, "purchase_orders", "ix_purchase_orders_company_document_hash"):
        op.drop_index("ix_purchase_orders_company_document_hash", table_name="purchase_orders")

    if _has_table(bind, "purchase_orders") and _has_column(bind, "purchase_orders", "source_document_hash"):
        op.drop_column("purchase_orders", "source_document_hash")
