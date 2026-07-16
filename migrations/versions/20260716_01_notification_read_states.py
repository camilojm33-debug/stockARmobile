"""Add notification read state table per user.

Revision ID: 20260716_01_notification_read_states
Revises: 20260715_04_cash_sessions_sales_link
Create Date: 2026-07-16
"""

from alembic import op
import sqlalchemy as sa


revision = "20260716_01_notification_read_states"
down_revision = "20260715_04_cash_sessions_sales_link"
branch_labels = None
depends_on = None


def _has_table(bind, table_name):
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def _has_index(bind, table_name, index_name):
    inspector = sa.inspect(bind)
    if table_name not in inspector.get_table_names():
        return False
    return index_name in {index["name"] for index in inspector.get_indexes(table_name)}


def upgrade() -> None:
    bind = op.get_bind()

    if not _has_table(bind, "notification_read_states"):
        op.create_table(
            "notification_read_states",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, unique=True),
            sa.Column("last_seen_signature", sa.String(length=64), nullable=False),
            sa.Column("last_seen_at", sa.DateTime(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
        )

    if _has_table(bind, "notification_read_states") and not _has_index(bind, "notification_read_states", "ix_notification_read_states_user_id"):
        op.create_index("ix_notification_read_states_user_id", "notification_read_states", ["user_id"], unique=True)


def downgrade() -> None:
    bind = op.get_bind()

    if _has_table(bind, "notification_read_states"):
        if _has_index(bind, "notification_read_states", "ix_notification_read_states_user_id"):
            op.drop_index("ix_notification_read_states_user_id", table_name="notification_read_states")
        op.drop_table("notification_read_states")
