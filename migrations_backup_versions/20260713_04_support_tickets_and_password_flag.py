"""Add support tickets and optional user password change flag.

Revision ID: 20260713_04
Revises: 20260713_03
Create Date: 2026-07-13
"""

from alembic import op
import sqlalchemy as sa


revision = "20260713_04"
down_revision = "20260713_03"
branch_labels = None
depends_on = None


def _has_table(bind, table_name):
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def _has_column(bind, table_name, column_name):
    inspector = sa.inspect(bind)
    if table_name not in inspector.get_table_names():
        return False
    return column_name in {column["name"] for column in inspector.get_columns(table_name)}


def _has_index(bind, table_name, index_name):
    inspector = sa.inspect(bind)
    if table_name not in inspector.get_table_names():
        return False
    return index_name in {index["name"] for index in inspector.get_indexes(table_name)}


def upgrade() -> None:
    bind = op.get_bind()

    if not _has_column(bind, "users", "must_change_password"):
        op.add_column("users", sa.Column("must_change_password", sa.Boolean(), nullable=False, server_default=sa.text("0")))

    if not _has_table(bind, "support_tickets"):
        op.create_table(
            "support_tickets",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("email", sa.String(length=160), nullable=False),
            sa.Column("reason", sa.String(length=80), nullable=False),
            sa.Column("description", sa.Text(), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="pendiente"),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("resolved_at", sa.DateTime(), nullable=True),
            sa.Column("resolved_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("resolved_note", sa.Text(), nullable=True),
        )

    if _has_table(bind, "support_tickets"):
        if not _has_index(bind, "support_tickets", "ix_support_tickets_company_id"):
            op.create_index("ix_support_tickets_company_id", "support_tickets", ["company_id"], unique=False)
        if not _has_index(bind, "support_tickets", "ix_support_tickets_user_id"):
            op.create_index("ix_support_tickets_user_id", "support_tickets", ["user_id"], unique=False)
        if not _has_index(bind, "support_tickets", "ix_support_tickets_reason"):
            op.create_index("ix_support_tickets_reason", "support_tickets", ["reason"], unique=False)
        if not _has_index(bind, "support_tickets", "ix_support_tickets_status"):
            op.create_index("ix_support_tickets_status", "support_tickets", ["status"], unique=False)
        if not _has_index(bind, "support_tickets", "ix_support_tickets_created_at"):
            op.create_index("ix_support_tickets_created_at", "support_tickets", ["created_at"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()

    if _has_table(bind, "support_tickets"):
        for idx in [
            "ix_support_tickets_created_at",
            "ix_support_tickets_status",
            "ix_support_tickets_reason",
            "ix_support_tickets_user_id",
            "ix_support_tickets_company_id",
        ]:
            if _has_index(bind, "support_tickets", idx):
                op.drop_index(idx, table_name="support_tickets")
        op.drop_table("support_tickets")

    if _has_column(bind, "users", "must_change_password"):
        op.drop_column("users", "must_change_password")
