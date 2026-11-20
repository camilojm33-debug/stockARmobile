"""Add tenant-side public marketing consent preferences and evidence.

Revision ID: 20261119_01_tenant_consent
Revises: 20261110_01_marketing_hardening
Create Date: 2026-11-19
"""
from alembic import op
import sqlalchemy as sa


revision = "20261119_01_tenant_consent"
down_revision = "20261110_01_marketing_hardening"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    columns = {item["name"] for item in sa.inspect(bind).get_columns("clients")}
    with op.batch_alter_table("clients") as batch:
        additions = (
            sa.Column("email_marketing_consent_recorded_at", sa.DateTime(), nullable=True),
            sa.Column("email_marketing_consent_source", sa.String(length=40), nullable=True),
            sa.Column("email_marketing_consent_evidence", sa.Text(), nullable=True),
            sa.Column("phone_marketing_consent", sa.String(length=20), nullable=False, server_default="unknown"),
            sa.Column("phone_marketing_consent_recorded_at", sa.DateTime(), nullable=True),
            sa.Column("phone_marketing_consent_source", sa.String(length=40), nullable=True),
            sa.Column("phone_marketing_consent_evidence", sa.Text(), nullable=True),
            sa.Column("marketing_consent_token", sa.String(length=96), nullable=True),
        )
        for column in additions:
            if column.name not in columns:
                batch.add_column(column)

    indexes = {item["name"] for item in sa.inspect(bind).get_indexes("clients")}
    if "ix_clients_phone_marketing_consent" not in indexes:
        op.create_index("ix_clients_phone_marketing_consent", "clients", ["phone_marketing_consent"])
    if "ix_clients_marketing_consent_token" not in indexes:
        op.create_index("ix_clients_marketing_consent_token", "clients", ["marketing_consent_token"], unique=True)


def downgrade():
    bind = op.get_bind()
    indexes = {item["name"] for item in sa.inspect(bind).get_indexes("clients")}
    if "ix_clients_marketing_consent_token" in indexes:
        op.drop_index("ix_clients_marketing_consent_token", table_name="clients")
    if "ix_clients_phone_marketing_consent" in indexes:
        op.drop_index("ix_clients_phone_marketing_consent", table_name="clients")
    columns = {item["name"] for item in sa.inspect(bind).get_columns("clients")}
    with op.batch_alter_table("clients") as batch:
        for name in (
            "marketing_consent_token",
            "phone_marketing_consent_evidence",
            "phone_marketing_consent_source",
            "phone_marketing_consent_recorded_at",
            "phone_marketing_consent",
            "email_marketing_consent_evidence",
            "email_marketing_consent_source",
            "email_marketing_consent_recorded_at",
        ):
            if name in columns:
                batch.drop_column(name)
