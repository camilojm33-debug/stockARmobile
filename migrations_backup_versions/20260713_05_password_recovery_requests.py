"""Add password recovery requests table.

Revision ID: 20260713_05
Revises: 20260713_04
Create Date: 2026-07-13
"""

from alembic import op
import sqlalchemy as sa


revision = "20260713_05"
down_revision = "20260713_04"
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

    if not _has_table(bind, "password_recovery_requests"):
        op.create_table(
            "password_recovery_requests",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("email", sa.String(length=160), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="pendiente"),
            sa.Column("requested_at", sa.DateTime(), nullable=True),
            sa.Column("processed_at", sa.DateTime(), nullable=True),
            sa.Column("processed_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        )

    if _has_table(bind, "password_recovery_requests"):
        if not _has_index(bind, "password_recovery_requests", "ix_password_recovery_requests_company_id"):
            op.create_index("ix_password_recovery_requests_company_id", "password_recovery_requests", ["company_id"], unique=False)
        if not _has_index(bind, "password_recovery_requests", "ix_password_recovery_requests_user_id"):
            op.create_index("ix_password_recovery_requests_user_id", "password_recovery_requests", ["user_id"], unique=False)
        if not _has_index(bind, "password_recovery_requests", "ix_password_recovery_requests_email"):
            op.create_index("ix_password_recovery_requests_email", "password_recovery_requests", ["email"], unique=False)
        if not _has_index(bind, "password_recovery_requests", "ix_password_recovery_requests_status"):
            op.create_index("ix_password_recovery_requests_status", "password_recovery_requests", ["status"], unique=False)
        if not _has_index(bind, "password_recovery_requests", "ix_password_recovery_requests_requested_at"):
            op.create_index("ix_password_recovery_requests_requested_at", "password_recovery_requests", ["requested_at"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()

    if _has_table(bind, "password_recovery_requests"):
        for idx in [
            "ix_password_recovery_requests_requested_at",
            "ix_password_recovery_requests_status",
            "ix_password_recovery_requests_email",
            "ix_password_recovery_requests_user_id",
            "ix_password_recovery_requests_company_id",
        ]:
            if _has_index(bind, "password_recovery_requests", idx):
                op.drop_index(idx, table_name="password_recovery_requests")
        op.drop_table("password_recovery_requests")
