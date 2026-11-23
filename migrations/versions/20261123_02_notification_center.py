"""Persist notifications for each account and company context.

Revision ID: 20261123_02_notification_center
Revises: 20261123_01_quote_item_pickup
Create Date: 2026-11-23
"""
from alembic import op
import sqlalchemy as sa

revision = "20261123_02_notification_center"
down_revision = "20261123_01_quote_item_pickup"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("notification_events"):
        return
    op.create_table(
        "notification_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=True),
        sa.Column("notification_key", sa.String(128), nullable=False),
        sa.Column("category", sa.String(40), nullable=False, server_default="general"),
        sa.Column("priority", sa.String(20), nullable=False, server_default="normal"),
        sa.Column("title", sa.String(180), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("href", sa.String(700), nullable=False),
        sa.Column("source_type", sa.String(80), nullable=True),
        sa.Column("source_id", sa.String(120), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("read_at", sa.DateTime(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("archived_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("user_id", "notification_key", name="uq_notification_events_user_key"),
    )
    op.create_index("ix_notification_events_user_created", "notification_events", ["user_id", "created_at"])
    op.create_index("ix_notification_events_company_created", "notification_events", ["company_id", "created_at"])
    op.create_index("ix_notification_events_user_state", "notification_events", ["user_id", "read_at", "resolved_at", "archived_at"])


def downgrade():
    op.drop_table("notification_events")
