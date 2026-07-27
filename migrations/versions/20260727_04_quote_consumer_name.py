"""add quote consumer name

Revision ID: 20260727_04_quote_consumer_name
Revises: 20260727_03_sync_audit_logs_ip_address
Create Date: 2026-07-27 17:10:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260727_04_quote_consumer_name"
down_revision = "20260727_03_sync_audit_logs_ip_address"
branch_labels = None
depends_on = None


def _has_column(bind, table_name, column_name):
    inspector = sa.inspect(bind)
    try:
        columns = inspector.get_columns(table_name)
    except Exception:
        return False
    return any(column.get("name") == column_name for column in columns)


def upgrade():
    bind = op.get_bind()
    if not _has_column(bind, "quotes", "consumer_name"):
        op.add_column("quotes", sa.Column("consumer_name", sa.String(length=160), nullable=True))


def downgrade():
    bind = op.get_bind()
    if _has_column(bind, "quotes", "consumer_name"):
        op.drop_column("quotes", "consumer_name")
