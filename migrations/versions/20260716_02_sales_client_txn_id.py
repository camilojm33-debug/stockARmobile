"""add sales client transaction id for idempotency

Revision ID: 20260716_02_sales_client_txn_id
Revises: 20260716_01_notification_read_states
Create Date: 2026-07-16 00:10:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260716_02_sales_client_txn_id"
down_revision = "20260716_01_notification_read_states"
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

    if _has_table(bind, "sales") and not _has_column(bind, "sales", "client_txn_id"):
        op.add_column("sales", sa.Column("client_txn_id", sa.String(length=64), nullable=True))

    if _has_table(bind, "sales") and not _has_index(bind, "sales", "ix_sales_company_client_txn"):
        op.create_index("ix_sales_company_client_txn", "sales", ["company_id", "client_txn_id"], unique=True)


def downgrade():
    bind = op.get_bind()

    if _has_table(bind, "sales") and _has_index(bind, "sales", "ix_sales_company_client_txn"):
        op.drop_index("ix_sales_company_client_txn", table_name="sales")

    if _has_table(bind, "sales") and _has_column(bind, "sales", "client_txn_id"):
        op.drop_column("sales", "client_txn_id")
