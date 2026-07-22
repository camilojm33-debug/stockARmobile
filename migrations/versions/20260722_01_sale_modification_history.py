"""add sale modification history table

Revision ID: 20260722_01_sale_modification_history
Revises: 20260720_01_expand_payments_external_reference
Create Date: 2026-07-22 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260722_01_sale_modification_history"
down_revision = "20260720_01_expand_payments_external_reference"
branch_labels = None
depends_on = None


def _has_table(bind, table_name):
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def upgrade():
    bind = op.get_bind()
    if not _has_table(bind, "sale_modification_history"):
        op.create_table(
            "sale_modification_history",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("sale_id", sa.Integer(), sa.ForeignKey("sales.id"), nullable=False),
            sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("previous_data", sa.Text(), nullable=False),
            sa.Column("new_data", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_sale_modification_history_sale_id", "sale_modification_history", ["sale_id"], unique=False)
        op.create_index("ix_sale_modification_history_company_id", "sale_modification_history", ["company_id"], unique=False)
        op.create_index("ix_sale_modification_history_user_id", "sale_modification_history", ["user_id"], unique=False)
        op.create_index("ix_sale_modification_history_created_at", "sale_modification_history", ["created_at"], unique=False)


def downgrade():
    bind = op.get_bind()
    if _has_table(bind, "sale_modification_history"):
        op.drop_index("ix_sale_modification_history_created_at", table_name="sale_modification_history")
        op.drop_index("ix_sale_modification_history_user_id", table_name="sale_modification_history")
        op.drop_index("ix_sale_modification_history_company_id", table_name="sale_modification_history")
        op.drop_index("ix_sale_modification_history_sale_id", table_name="sale_modification_history")
        op.drop_table("sale_modification_history")
