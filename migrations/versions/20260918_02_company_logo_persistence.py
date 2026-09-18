"""Persist company logos in PostgreSQL instead of the Render filesystem.

Revision ID: 20260918_02_company_logo_persistence
Revises: 20260918_01_ai_order_delivery
"""

from alembic import op
import sqlalchemy as sa


revision = "20260918_02_company_logo_persistence"
down_revision = "20260918_01_ai_order_delivery"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("companies", schema=None) as batch_op:
        batch_op.add_column(sa.Column("logo_data", sa.LargeBinary(), nullable=True))
        batch_op.add_column(sa.Column("logo_mime_type", sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column("logo_public_token", sa.String(length=64), nullable=True))
        batch_op.create_index("ix_companies_logo_public_token", ["logo_public_token"], unique=True)


def downgrade():
    with op.batch_alter_table("companies", schema=None) as batch_op:
        batch_op.drop_index("ix_companies_logo_public_token")
        batch_op.drop_column("logo_public_token")
        batch_op.drop_column("logo_mime_type")
        batch_op.drop_column("logo_data")
