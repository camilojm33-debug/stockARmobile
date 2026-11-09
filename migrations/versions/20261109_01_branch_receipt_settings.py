"""Persist per-branch receipt identity and printer overrides.

Revision ID: 20261109_01_branch_receipts
Revises: 20261106_01_branch_vendor
Create Date: 2026-11-09
"""
from alembic import op
import sqlalchemy as sa


revision = "20261109_01_branch_receipts"
down_revision = "20261106_01_branch_vendor"
branch_labels = None
depends_on = None


def upgrade():
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns("branches")}
    if "receipt_settings_json" not in columns:
        op.add_column("branches", sa.Column("receipt_settings_json", sa.Text(), nullable=True))


def downgrade():
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns("branches")}
    if "receipt_settings_json" in columns:
        op.drop_column("branches", "receipt_settings_json")
