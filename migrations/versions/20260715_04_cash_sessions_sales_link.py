"""Link sales to cash sessions and add cash lifecycle fields.

Revision ID: 20260715_04_cash_sessions_sales_link
Revises: 20260715_03_mercadopago_oauth_connections
Create Date: 2026-07-15
"""

from alembic import op
import sqlalchemy as sa


revision = "20260715_04_cash_sessions_sales_link"
down_revision = "20260715_03_mercadopago_oauth_connections"
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


def upgrade() -> None:
    bind = op.get_bind()

    if _has_table(bind, "sales") and not _has_column(bind, "sales", "cash_session_id"):
        op.add_column("sales", sa.Column("cash_session_id", sa.Integer(), sa.ForeignKey("cash_sessions.id"), nullable=True))
        if not _has_index(bind, "sales", "ix_sales_cash_session_id"):
            op.create_index("ix_sales_cash_session_id", "sales", ["cash_session_id"], unique=False)

    if _has_table(bind, "cash_sessions"):
        cash_session_columns = [
            ("opened_by_user_id", sa.Integer(), True, None),
            ("counted_amount", sa.Numeric(12, 2), True, None),
            ("difference_amount", sa.Numeric(12, 2), True, None),
            ("closing_note", sa.Text(), True, None),
            ("reopened_at", sa.DateTime(), True, None),
            ("reopened_by_user_id", sa.Integer(), True, None),
            ("voided_at", sa.DateTime(), True, None),
            ("voided_by_user_id", sa.Integer(), True, None),
            ("void_reason", sa.Text(), True, None),
        ]
        for name, col_type, nullable, server_default in cash_session_columns:
            if not _has_column(bind, "cash_sessions", name):
                op.add_column("cash_sessions", sa.Column(name, col_type, nullable=nullable, server_default=server_default))

    if _has_table(bind, "cash_movements") and not _has_column(bind, "cash_movements", "sale_id"):
        op.add_column("cash_movements", sa.Column("sale_id", sa.Integer(), sa.ForeignKey("sales.id"), nullable=True))
        if not _has_index(bind, "cash_movements", "ix_cash_movements_sale_id"):
            op.create_index("ix_cash_movements_sale_id", "cash_movements", ["sale_id"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()

    if _has_table(bind, "cash_movements"):
        if _has_index(bind, "cash_movements", "ix_cash_movements_sale_id"):
            op.drop_index("ix_cash_movements_sale_id", table_name="cash_movements")
        if _has_column(bind, "cash_movements", "sale_id"):
            op.drop_column("cash_movements", "sale_id")

    if _has_table(bind, "cash_sessions"):
        for name in [
            "void_reason",
            "voided_by_user_id",
            "voided_at",
            "reopened_by_user_id",
            "reopened_at",
            "closing_note",
            "difference_amount",
            "counted_amount",
            "opened_by_user_id",
        ]:
            if _has_column(bind, "cash_sessions", name):
                op.drop_column("cash_sessions", name)

    if _has_table(bind, "sales"):
        if _has_index(bind, "sales", "ix_sales_cash_session_id"):
            op.drop_index("ix_sales_cash_session_id", table_name="sales")
        if _has_column(bind, "sales", "cash_session_id"):
            op.drop_column("sales", "cash_session_id")
