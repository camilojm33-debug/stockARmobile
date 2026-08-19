"""Store discount and surcharge intent for sales and quotes.

Revision ID: 20260818_01_order_adjustment_metadata
Revises: 20260813_02_fix_conversation_messages_id_autoincrement
Create Date: 2026-08-18
"""

from alembic import op
import sqlalchemy as sa


revision = "20260818_01_order_adjustment_metadata"
down_revision = "20260813_02_fix_conversation_messages_id_autoincrement"
branch_labels = None
depends_on = None


def _has_column(bind, table_name, column_name):
    return any(column["name"] == column_name for column in sa.inspect(bind).get_columns(table_name))


def _add_columns(table_name):
    bind = op.get_bind()
    columns = {
        "discount_type": sa.Column("discount_type", sa.String(length=20), nullable=True),
        "discount_value": sa.Column("discount_value", sa.Numeric(18, 2), nullable=True),
        "discount_reason": sa.Column("discount_reason", sa.Text(), nullable=True),
        "surcharge_type": sa.Column("surcharge_type", sa.String(length=20), nullable=True),
        "surcharge_value": sa.Column("surcharge_value", sa.Numeric(18, 2), nullable=True),
        "surcharge_reason": sa.Column("surcharge_reason", sa.Text(), nullable=True),
    }
    for name, column in columns.items():
        if not _has_column(bind, table_name, name):
            op.add_column(table_name, column)


def upgrade():
    _add_columns("sales")
    _add_columns("quotes")


def downgrade():
    bind = op.get_bind()
    for table_name in ("quotes", "sales"):
        for column_name in (
            "surcharge_reason",
            "surcharge_value",
            "surcharge_type",
            "discount_reason",
            "discount_value",
            "discount_type",
        ):
            if _has_column(bind, table_name, column_name):
                op.drop_column(table_name, column_name)