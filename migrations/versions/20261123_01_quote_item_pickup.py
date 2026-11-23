"""Track partial and complete pickup quantities per quote line.

Revision ID: 20261123_01_quote_item_pickup
Revises: 20261119_01_tenant_consent
Create Date: 2026-11-23
"""
from alembic import op
import sqlalchemy as sa

revision = "20261123_01_quote_item_pickup"
down_revision = "20261119_01_tenant_consent"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("quote_item_pickup_events"):
        return
    op.create_table(
        "quote_item_pickup_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("quote_id", sa.Integer(), sa.ForeignKey("quotes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("quote_item_id", sa.Integer(), sa.ForeignKey("quote_items.id", ondelete="CASCADE"), nullable=False),
        sa.Column("quantity", sa.Numeric(12, 3), nullable=False),
        sa.Column("picked_up_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("picked_up_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("note", sa.String(length=500), nullable=True),
    )
    op.create_index("ix_quote_item_pickup_company_quote", "quote_item_pickup_events", ["company_id", "quote_id"])
    op.create_index("ix_quote_item_pickup_line", "quote_item_pickup_events", ["quote_item_id", "picked_up_at"])
    op.create_index("ix_quote_item_pickup_events_company_id", "quote_item_pickup_events", ["company_id"])
    op.create_index("ix_quote_item_pickup_events_quote_id", "quote_item_pickup_events", ["quote_id"])
    op.create_index("ix_quote_item_pickup_events_quote_item_id", "quote_item_pickup_events", ["quote_item_id"])
    op.create_index("ix_quote_item_pickup_events_picked_up_at", "quote_item_pickup_events", ["picked_up_at"])


def downgrade():
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("quote_item_pickup_events"):
        return
    op.drop_table("quote_item_pickup_events")
