"""Persist individual notification read keys per user.

Revision ID: 20260924_02_notification_individual_reads
Revises: 20260924_01_vendor_checkout_charges
"""

from alembic import op
import sqlalchemy as sa


revision = "20260924_02_notification_individual_reads"
down_revision = "20260924_01_vendor_checkout_charges"
branch_labels = None
depends_on = None


def _has_column(bind, table_name, column_name):
    inspector = sa.inspect(bind)
    return column_name in {column["name"] for column in inspector.get_columns(table_name)}


def upgrade():
    bind = op.get_bind()
    if not _has_column(bind, "notification_read_states", "read_notification_keys"):
        op.add_column(
            "notification_read_states",
            sa.Column("read_notification_keys", sa.Text(), nullable=True),
        )


def downgrade():
    bind = op.get_bind()
    if _has_column(bind, "notification_read_states", "read_notification_keys"):
        op.drop_column("notification_read_states", "read_notification_keys")
