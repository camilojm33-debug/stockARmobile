"""Add encrypted server-side one-time secret storage.

Revision ID: 20260920_01_one_time_secrets
Revises: 20260918_02_company_logo_persistence
"""
from alembic import op
import sqlalchemy as sa


revision = "20260920_01_one_time_secrets"
down_revision = "20260918_02_company_logo_persistence"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "one_time_secrets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("purpose", sa.String(length=80), nullable=False, index=True),
        sa.Column("subject_type", sa.String(length=40), nullable=False),
        sa.Column("subject_id", sa.Integer(), nullable=True, index=True),
        sa.Column("access_token_hash", sa.String(length=128), nullable=False, unique=True, index=True),
        sa.Column("ciphertext", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False, index=True),
        sa.Column("consumed_at", sa.DateTime(), nullable=True, index=True),
        sa.Column("created_at", sa.DateTime(), nullable=True, index=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_one_time_secrets_owner",
        "one_time_secrets",
        ["user_id", "purpose", "subject_id"],
        unique=False,
    )


def downgrade():
    op.drop_index("ix_one_time_secrets_owner", table_name="one_time_secrets")
    op.drop_table("one_time_secrets")
