"""subscription commands hardening

Revision ID: 20260729_01_subscription_commands_hardening
Revises: 20260727_04_quote_consumer_name
Create Date: 2026-07-29 21:30:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260729_01_subscription_commands_hardening"
down_revision = "20260727_04_quote_consumer_name"
branch_labels = None
depends_on = None


def _table_exists(bind, table_name):
    inspector = sa.inspect(bind)
    try:
        return table_name in inspector.get_table_names()
    except Exception:
        return False


def _index_exists(bind, table_name, index_name):
    inspector = sa.inspect(bind)
    try:
        indexes = inspector.get_indexes(table_name)
    except Exception:
        return False
    return any((item.get("name") or "") == index_name for item in indexes)


def upgrade():
    bind = op.get_bind()

    if not _table_exists(bind, "subscription_command_executions"):
        op.create_table(
            "subscription_command_executions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("command_name", sa.String(length=80), nullable=False),
            sa.Column("command_key", sa.String(length=180), nullable=False),
            sa.Column("command_status", sa.String(length=30), nullable=False, server_default="completed"),
            sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False),
            sa.Column("subscription_id", sa.Integer(), sa.ForeignKey("subscriptions.id"), nullable=True),
            sa.Column("actor_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("origin", sa.String(length=40), nullable=True, server_default="system"),
            sa.Column("ip_address", sa.String(length=120), nullable=True),
            sa.Column("payload_json", sa.Text(), nullable=True),
            sa.Column("result_json", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
        )

    if not _index_exists(bind, "subscription_command_executions", "ix_subscription_command_company_created"):
        op.create_index(
            "ix_subscription_command_company_created",
            "subscription_command_executions",
            ["company_id", "created_at"],
            unique=False,
        )
    if not _index_exists(bind, "subscription_command_executions", "ix_subscription_command_executions_command_key"):
        op.create_index(
            "ix_subscription_command_executions_command_key",
            "subscription_command_executions",
            ["command_key"],
            unique=True,
        )

    # Partial unique index: one active subscription per company.
    dialect_name = bind.dialect.name
    if not _index_exists(bind, "subscriptions", "uq_subscriptions_single_active_company"):
        if dialect_name == "postgresql":
            op.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_subscriptions_single_active_company ON subscriptions(company_id) WHERE status = 'active'"
            )
        elif dialect_name == "sqlite":
            op.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_subscriptions_single_active_company ON subscriptions(company_id) WHERE status = 'active'"
            )


def downgrade():
    bind = op.get_bind()

    if _index_exists(bind, "subscriptions", "uq_subscriptions_single_active_company"):
        op.drop_index("uq_subscriptions_single_active_company", table_name="subscriptions")

    if _index_exists(bind, "subscription_command_executions", "ix_subscription_command_executions_command_key"):
        op.drop_index("ix_subscription_command_executions_command_key", table_name="subscription_command_executions")

    if _index_exists(bind, "subscription_command_executions", "ix_subscription_command_company_created"):
        op.drop_index("ix_subscription_command_company_created", table_name="subscription_command_executions")

    if _table_exists(bind, "subscription_command_executions"):
        op.drop_table("subscription_command_executions")
