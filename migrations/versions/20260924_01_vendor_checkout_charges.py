"""Add a persisted charge snapshot to AI vendor quotes.

Revision ID: 20260924_01_vendor_checkout_charges
Revises: 20260922_06_client_quote_join_indexes
"""

from alembic import op
import sqlalchemy as sa


revision = "20260924_01_vendor_checkout_charges"
down_revision = "20260922_06_client_quote_join_indexes"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("quotes", sa.Column("charges_json", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("quotes", "charges_json")
