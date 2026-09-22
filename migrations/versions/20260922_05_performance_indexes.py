"""Add performance indexes for dashboard and common tenant-scoped joins.

Revision ID: 20260922_05_performance_indexes
Revises: 20260922_04_mercadopago_company_isolation
"""

from alembic import op
import sqlalchemy as sa


revision = "20260922_05_performance_indexes"
down_revision = "20260922_04_mercadopago_company_isolation"
branch_labels = None
depends_on = None


def _has_table(bind, table_name):
    return table_name in sa.inspect(bind).get_table_names()


def _has_index(bind, table_name, index_name):
    inspector = sa.inspect(bind)
    if table_name not in inspector.get_table_names():
        return False
    return index_name in {index["name"] for index in inspector.get_indexes(table_name)}


def _create_index(bind, name, table, columns):
    if _has_table(bind, table) and not _has_index(bind, table, name):
        op.create_index(name, table, columns)


def _drop_index(bind, name, table):
    if _has_table(bind, table) and _has_index(bind, table, name):
        op.drop_index(name, table_name=table)


def upgrade() -> None:
    bind = op.get_bind()

    _create_index(bind, "ix_sales_company_date", "sales", ["company_id", "date"])
    _create_index(bind, "ix_sale_items_sale_id", "sale_items", ["sale_id"])
    _create_index(bind, "ix_sale_items_product_id", "sale_items", ["product_id"])

    _create_index(bind, "ix_products_company_active_stock", "products", ["company_id", "active", "stock"])
    _create_index(bind, "ix_clients_company_active_created", "clients", ["company_id", "active", "created_at"])
    _create_index(bind, "ix_expenses_company_date", "expenses", ["company_id", "date"])
    _create_index(bind, "ix_cash_sessions_company_status_closed", "cash_sessions", ["company_id", "status", "closed_at"])


def downgrade() -> None:
    bind = op.get_bind()

    _drop_index(bind, "ix_cash_sessions_company_status_closed", "cash_sessions")
    _drop_index(bind, "ix_expenses_company_date", "expenses")
    _drop_index(bind, "ix_clients_company_active_created", "clients")
    _drop_index(bind, "ix_products_company_active_stock", "products")
    _drop_index(bind, "ix_sale_items_product_id", "sale_items")
    _drop_index(bind, "ix_sale_items_sale_id", "sale_items")
    _drop_index(bind, "ix_sales_company_date", "sales")
