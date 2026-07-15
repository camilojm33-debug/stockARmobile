"""Expand backup logs for full backup module.

Revision ID: 20260715_02_backup_logs_full_module
Revises: 20260715_01_password_reset_tokens
Create Date: 2026-07-15
"""

from alembic import op
import sqlalchemy as sa


revision = "20260715_02_backup_logs_full_module"
down_revision = "20260715_01_password_reset_tokens"
branch_labels = None
depends_on = None


def _has_table(bind, table_name):
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def _has_column(bind, table_name, column_name):
    inspector = sa.inspect(bind)
    if table_name not in inspector.get_table_names():
        return False
    return column_name in {col["name"] for col in inspector.get_columns(table_name)}


def _has_index(bind, table_name, index_name):
    inspector = sa.inspect(bind)
    if table_name not in inspector.get_table_names():
        return False
    return index_name in {idx["name"] for idx in inspector.get_indexes(table_name)}


def upgrade() -> None:
    bind = op.get_bind()

    if not _has_table(bind, "backup_logs"):
        op.create_table(
            "backup_logs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id")),
            sa.Column("status", sa.String(length=30), nullable=True, server_default="pendiente"),
            sa.Column("trigger_type", sa.String(length=30), nullable=False, server_default="manual"),
            sa.Column("plan_code", sa.String(length=40), nullable=True),
            sa.Column("file_name", sa.String(length=255), nullable=True),
            sa.Column("file_size_bytes", sa.BigInteger(), nullable=True, server_default="0"),
            sa.Column("path", sa.String(length=255), nullable=True),
            sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("restored_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("restored_at", sa.DateTime(), nullable=True),
            sa.Column("is_automated", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("next_run_at", sa.DateTime(), nullable=True),
            sa.Column("metadata_json", sa.Text(), nullable=True),
            sa.Column("detail", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
        )

    if _has_table(bind, "backup_logs"):
        columns_to_add = [
            ("trigger_type", sa.Column("trigger_type", sa.String(length=30), nullable=False, server_default="manual")),
            ("plan_code", sa.Column("plan_code", sa.String(length=40), nullable=True)),
            ("file_name", sa.Column("file_name", sa.String(length=255), nullable=True)),
            ("file_size_bytes", sa.Column("file_size_bytes", sa.BigInteger(), nullable=True, server_default="0")),
            ("created_by_user_id", sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True)),
            ("restored_by_user_id", sa.Column("restored_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True)),
            ("restored_at", sa.Column("restored_at", sa.DateTime(), nullable=True)),
            ("is_automated", sa.Column("is_automated", sa.Boolean(), nullable=False, server_default=sa.text("false"))),
            ("next_run_at", sa.Column("next_run_at", sa.DateTime(), nullable=True)),
            ("metadata_json", sa.Column("metadata_json", sa.Text(), nullable=True)),
        ]
        for name, column in columns_to_add:
            if not _has_column(bind, "backup_logs", name):
                op.add_column("backup_logs", column)

        if not _has_index(bind, "backup_logs", "ix_backup_logs_created_at"):
            op.create_index("ix_backup_logs_created_at", "backup_logs", ["created_at"], unique=False)
        if not _has_index(bind, "backup_logs", "ix_backup_logs_plan_code"):
            op.create_index("ix_backup_logs_plan_code", "backup_logs", ["plan_code"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    if not _has_table(bind, "backup_logs"):
        return

    for idx in ["ix_backup_logs_plan_code", "ix_backup_logs_created_at"]:
        if _has_index(bind, "backup_logs", idx):
            op.drop_index(idx, table_name="backup_logs")

    for col in [
        "metadata_json",
        "next_run_at",
        "is_automated",
        "restored_at",
        "restored_by_user_id",
        "created_by_user_id",
        "file_size_bytes",
        "file_name",
        "plan_code",
        "trigger_type",
    ]:
        if _has_column(bind, "backup_logs", col):
            op.drop_column("backup_logs", col)
