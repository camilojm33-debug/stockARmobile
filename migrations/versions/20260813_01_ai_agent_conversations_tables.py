"""AI Agent conversation tables

Revision ID: 20260813_01_ai_agent_conversations_tables
Revises: 20260806_01_harden_purchases_company_scope
Create Date: 2026-08-13

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "20260813_01_ai_agent_conversations_tables"
down_revision = "20260806_01_harden_purchases_company_scope"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "agents",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agents_company_id", "agents", ["company_id"], unique=False)

    op.create_table(
        "agent_configurations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("agent_id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("model", sa.String(length=120), nullable=False),
        sa.Column("system_prompt", sa.Text(), nullable=True),
        sa.Column("language", sa.String(length=8), nullable=True),
        sa.Column("max_tokens", sa.Integer(), nullable=True),
        sa.Column("temperature", sa.Numeric(precision=4, scale=3), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"]),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agentcfg_agent_id", "agent_configurations", ["agent_id"], unique=False)
    op.create_index("ix_agentcfg_company_id", "agent_configurations", ["company_id"], unique=False)

    op.create_table(
        "conversations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("agent_id", sa.Integer(), nullable=True),
        sa.Column("channel", sa.String(length=60), nullable=False),
        sa.Column("external_conversation_id", sa.String(length=200), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False, server_default=sa.text("'open'")),
        sa.Column("metadata_json", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_conversations_company_channel", "conversations", ["company_id", "channel"], unique=False)
    op.create_index("ix_conversations_company_external_id", "conversations", ["company_id", "external_conversation_id"], unique=False)
    op.create_index("ix_conversations_company_status", "conversations", ["company_id", "status"], unique=False)

    op.create_table(
        "conversation_messages",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("sender_type", sa.String(length=30), nullable=False),
        sa.Column("sender_id", sa.Integer(), nullable=True),
        sa.Column("role", sa.String(length=30), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_json", postgresql.JSONB(), nullable=True),
        sa.Column("content_type", sa.String(length=40), nullable=False, server_default=sa.text("'text'")),
        sa.Column("external_message_id", sa.String(length=255), nullable=True),
        sa.Column("idempotency_key", sa.String(length=120), nullable=True),
        sa.Column("trace_id", sa.String(length=120), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("delivered_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"]),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_convmsg_external_company_conv",
        "conversation_messages",
        ["company_id", "conversation_id", "external_message_id"],
        unique=True,
        postgresql_where=sa.text("external_message_id IS NOT NULL"),
    )
    op.create_index(
        "uq_convmsg_company_idempotency",
        "conversation_messages",
        ["company_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.create_index("ix_convmsgs_company_conversation", "conversation_messages", ["company_id", "conversation_id"], unique=False)
    op.create_index("ix_convmsgs_company_created", "conversation_messages", ["company_id", "created_at"], unique=False)

    op.create_table(
        "conversation_participants",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("participant_type", sa.String(length=40), nullable=False),
        sa.Column("participant_id", sa.Integer(), nullable=True),
        sa.Column("display_name", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"]),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_convparts_company_conv", "conversation_participants", ["company_id", "conversation_id"], unique=False)


def downgrade():
    op.drop_index("ix_convparts_company_conv", table_name="conversation_participants")
    op.drop_table("conversation_participants")

    op.drop_index("ix_convmsgs_company_created", table_name="conversation_messages")
    op.drop_index("ix_convmsgs_company_conversation", table_name="conversation_messages")
    op.drop_index("uq_convmsg_company_idempotency", table_name="conversation_messages")
    op.drop_index("uq_convmsg_external_company_conv", table_name="conversation_messages")
    op.drop_table("conversation_messages")

    op.drop_index("ix_conversations_company_status", table_name="conversations")
    op.drop_index("ix_conversations_company_external_id", table_name="conversations")
    op.drop_index("ix_conversations_company_channel", table_name="conversations")
    op.drop_table("conversations")

    op.drop_index("ix_agentcfg_company_id", table_name="agent_configurations")
    op.drop_index("ix_agentcfg_agent_id", table_name="agent_configurations")
    op.drop_table("agent_configurations")

    op.drop_index("ix_agents_company_id", table_name="agents")
    op.drop_table("agents")
