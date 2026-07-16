"""Add per-company Mercado Pago OAuth connections.

Revision ID: 20260715_03_mercadopago_oauth_connections
Revises: 20260715_02_backup_logs_full_module
Create Date: 2026-07-15
"""

from alembic import op
import sqlalchemy as sa


revision = "20260715_03_mercadopago_oauth_connections"
down_revision = "20260715_02_backup_logs_full_module"
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

    if not _has_table(bind, "mercadopago_connections"):
        op.create_table(
            "mercadopago_connections",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False),
            sa.Column("mp_user_id", sa.String(length=80), nullable=True),
            sa.Column("account_name", sa.String(length=160), nullable=True),
            sa.Column("account_email", sa.String(length=160), nullable=True),
            sa.Column("country", sa.String(length=80), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="disconnected"),
            sa.Column("connected_at", sa.DateTime(), nullable=True),
            sa.Column("last_synced_at", sa.DateTime(), nullable=True),
            sa.Column("token_expires_at", sa.DateTime(), nullable=True),
            sa.Column("access_token_encrypted", sa.Text(), nullable=True),
            sa.Column("refresh_token_encrypted", sa.Text(), nullable=True),
            sa.Column("scope", sa.String(length=255), nullable=True),
            sa.Column("metadata_json", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
        )

    if _has_table(bind, "mercadopago_connections"):
        if not _has_index(bind, "mercadopago_connections", "ix_mercadopago_connections_company_id"):
            op.create_index("ix_mercadopago_connections_company_id", "mercadopago_connections", ["company_id"], unique=True)
        if not _has_index(bind, "mercadopago_connections", "ix_mercadopago_connections_mp_user_id"):
            op.create_index("ix_mercadopago_connections_mp_user_id", "mercadopago_connections", ["mp_user_id"], unique=False)
        if not _has_index(bind, "mercadopago_connections", "ix_mercadopago_connections_account_email"):
            op.create_index("ix_mercadopago_connections_account_email", "mercadopago_connections", ["account_email"], unique=False)
        if not _has_index(bind, "mercadopago_connections", "ix_mercadopago_connections_status"):
            op.create_index("ix_mercadopago_connections_status", "mercadopago_connections", ["status"], unique=False)
        if not _has_index(bind, "mercadopago_connections", "ix_mercadopago_connections_token_expires_at"):
            op.create_index("ix_mercadopago_connections_token_expires_at", "mercadopago_connections", ["token_expires_at"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()

    if _has_table(bind, "mercadopago_connections"):
        for idx in [
            "ix_mercadopago_connections_token_expires_at",
            "ix_mercadopago_connections_status",
            "ix_mercadopago_connections_account_email",
            "ix_mercadopago_connections_mp_user_id",
            "ix_mercadopago_connections_company_id",
        ]:
            if _has_index(bind, "mercadopago_connections", idx):
                op.drop_index(idx, table_name="mercadopago_connections")
        op.drop_table("mercadopago_connections")
