"""Ensure webhook idempotency and product modification history tables exist.

Revision ID: 20261024_01_release_readiness
Revises: 20261016_01_crm_loss_reason
Create Date: 2026-10-24
"""
from alembic import op
import sqlalchemy as sa


revision = "20261024_01_release_readiness"
down_revision = "20261016_01_crm_loss_reason"
branch_labels = None
depends_on = None


def _has_table(bind, table_name):
    return sa.inspect(bind).has_table(table_name)


def _has_index(bind, table_name, index_name):
    inspector = sa.inspect(bind)
    if not inspector.has_table(table_name):
        return False
    return any(index.get("name") == index_name for index in inspector.get_indexes(table_name))


def _ensure_index(bind, table_name, index_name, column_name, *, unique=False):
    if _has_table(bind, table_name) and not _has_index(bind, table_name, index_name):
        columns = {column["name"] for column in sa.inspect(bind).get_columns(table_name)}
        if column_name in columns:
            op.create_index(index_name, table_name, [column_name], unique=unique)


def upgrade():
    bind = op.get_bind()

    # Some existing installations created these model tables through metadata
    # bootstrap, while a clean Alembic installation did not have a dedicated
    # revision for them. Create only what is absent; never replace existing data.
    if not _has_table(bind, "webhook_events"):
        op.create_table(
            "webhook_events",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("provider", sa.String(length=40), nullable=False, server_default="mercadopago"),
            sa.Column("event_key", sa.String(length=180), nullable=False),
            sa.Column("event_type", sa.String(length=80), nullable=False),
            sa.Column("status", sa.String(length=30), nullable=True, server_default="processed"),
            sa.Column("payload_json", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
        )

    if not _has_table(bind, "product_modifications"):
        op.create_table(
            "product_modifications",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=False),
            sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("action", sa.String(length=80), nullable=False),
            sa.Column("detail", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
        )

    _ensure_index(bind, "webhook_events", "ix_webhook_events_event_key", "event_key", unique=True)
    _ensure_index(bind, "webhook_events", "ix_webhook_events_status", "status")
    _ensure_index(bind, "webhook_events", "ix_webhook_events_created_at", "created_at")
    _ensure_index(bind, "product_modifications", "ix_product_modifications_created_at", "created_at")


def downgrade():
    # This compatibility migration also repairs installations where these
    # tables pre-existed Alembic. Dropping them during downgrade could destroy
    # existing production data, so it intentionally leaves the tables intact.
    pass
