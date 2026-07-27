"""sync audit_logs ip_address column

Revision ID: 20260727_03_sync_audit_logs_ip_address
Revises: 20260727_02_merge_crm_and_quotes_heads
Create Date: 2026-07-27 16:10:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260727_03_sync_audit_logs_ip_address"
down_revision = "20260727_02_merge_crm_and_quotes_heads"
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
    if _has_table(bind, "audit_logs") and not _has_column(bind, "audit_logs", "ip_address"):
        op.add_column("audit_logs", sa.Column("ip_address", sa.String(length=45), nullable=True))


def downgrade():
    bind = op.get_bind()
    if _has_table(bind, "audit_logs") and _has_column(bind, "audit_logs", "ip_address"):
        op.drop_column("audit_logs", "ip_address")
