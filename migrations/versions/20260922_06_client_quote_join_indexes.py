"""Add indexes for common tenant-scoped client/quote joins.

Revision ID: 20260922_06_client_quote_join_indexes
Revises: 20260922_05_performance_indexes
"""

from alembic import op
import sqlalchemy as sa


revision = "20260922_06_client_quote_join_indexes"
down_revision = "20260922_05_performance_indexes"
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


def upgrade():
    bind = op.get_bind()

    # These are read-only performance indexes. They do not change query results,
    # tenant scoping, ordering, or business rules.
    _create_index(bind, "ix_sales_company_client", "sales", ["company_id", "client_id"])
    _create_index(bind, "ix_quotes_company_client", "quotes", ["company_id", "client_id"])


def downgrade():
    bind = op.get_bind()

    _drop_index(bind, "ix_quotes_company_client", "quotes")
    _drop_index(bind, "ix_sales_company_client", "sales")
