"""Add password reset tokens table.

Revision ID: 20260715_01_password_reset_tokens
Revises: 20260714_09_company_modules_fields
Create Date: 2026-07-15
"""

from alembic import op
import sqlalchemy as sa


revision = "20260715_01_password_reset_tokens"
down_revision = "20260714_09_company_modules_fields"
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

    if not _has_table(bind, "password_reset_tokens"):
        op.create_table(
            "password_reset_tokens",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("email", sa.String(length=160), nullable=False),
            sa.Column("token_hash", sa.String(length=128), nullable=False),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("used_at", sa.DateTime(), nullable=True),
            sa.Column("revoked_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
        )

    if _has_table(bind, "password_reset_tokens"):
        if not _has_index(bind, "password_reset_tokens", "ix_password_reset_tokens_user_id"):
            op.create_index("ix_password_reset_tokens_user_id", "password_reset_tokens", ["user_id"], unique=False)
        if not _has_index(bind, "password_reset_tokens", "ix_password_reset_tokens_email"):
            op.create_index("ix_password_reset_tokens_email", "password_reset_tokens", ["email"], unique=False)
        if not _has_index(bind, "password_reset_tokens", "ix_password_reset_tokens_token_hash"):
            op.create_index("ix_password_reset_tokens_token_hash", "password_reset_tokens", ["token_hash"], unique=True)
        if not _has_index(bind, "password_reset_tokens", "ix_password_reset_tokens_expires_at"):
            op.create_index("ix_password_reset_tokens_expires_at", "password_reset_tokens", ["expires_at"], unique=False)
        if not _has_index(bind, "password_reset_tokens", "ix_password_reset_tokens_used_at"):
            op.create_index("ix_password_reset_tokens_used_at", "password_reset_tokens", ["used_at"], unique=False)
        if not _has_index(bind, "password_reset_tokens", "ix_password_reset_tokens_revoked_at"):
            op.create_index("ix_password_reset_tokens_revoked_at", "password_reset_tokens", ["revoked_at"], unique=False)
        if not _has_index(bind, "password_reset_tokens", "ix_password_reset_tokens_created_at"):
            op.create_index("ix_password_reset_tokens_created_at", "password_reset_tokens", ["created_at"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()

    if _has_table(bind, "password_reset_tokens"):
        for idx in [
            "ix_password_reset_tokens_created_at",
            "ix_password_reset_tokens_revoked_at",
            "ix_password_reset_tokens_used_at",
            "ix_password_reset_tokens_expires_at",
            "ix_password_reset_tokens_token_hash",
            "ix_password_reset_tokens_email",
            "ix_password_reset_tokens_user_id",
        ]:
            if _has_index(bind, "password_reset_tokens", idx):
                op.drop_index(idx, table_name="password_reset_tokens")
        op.drop_table("password_reset_tokens")
