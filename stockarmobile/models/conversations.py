"""SQLAlchemy models for AI Agent / Conversations (declaration-only)."""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from stockarmobile.extensions import db
from stockarmobile.helpers.dates import utcnow_naive as utcnow

JSONType = sa.JSON().with_variant(JSONB, "postgresql")


class Agent(db.Model):
    __tablename__ = "agents"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(
        db.Integer,
        db.ForeignKey("companies.id"),
        nullable=False,
    )
    name = db.Column(db.String(160), nullable=False)
    description = db.Column(db.Text)
    active = db.Column(
        db.Boolean,
        nullable=False,
        server_default=sa.text("true"),
    )
    created_at = db.Column(
        db.DateTime,
        default=utcnow,
        nullable=False,
    )
    updated_at = db.Column(
        db.DateTime,
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
    )

    __table_args__ = (
        sa.Index("ix_agents_company_id", "company_id"),
    )


class AgentConfiguration(db.Model):
    __tablename__ = "agent_configurations"

    id = db.Column(db.Integer, primary_key=True)
    agent_id = db.Column(
        db.Integer,
        db.ForeignKey("agents.id"),
        nullable=False,
    )
    company_id = db.Column(
        db.Integer,
        db.ForeignKey("companies.id"),
        nullable=False,
    )
    model = db.Column(db.String(120), nullable=False)
    system_prompt = db.Column(db.Text)
    language = db.Column(db.String(8))
    max_tokens = db.Column(db.Integer)
    temperature = db.Column(db.Numeric(4, 3))
    created_at = db.Column(
        db.DateTime,
        default=utcnow,
        nullable=False,
    )
    updated_at = db.Column(
        db.DateTime,
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
    )

    __table_args__ = (
        sa.Index("ix_agentcfg_agent_id", "agent_id"),
        sa.Index("ix_agentcfg_company_id", "company_id"),
    )


class Conversation(db.Model):
    __tablename__ = "conversations"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(
        db.Integer,
        db.ForeignKey("companies.id"),
        nullable=False,
    )
    agent_id = db.Column(
        db.Integer,
        db.ForeignKey("agents.id"),
        nullable=True,
    )
    channel = db.Column(db.String(60), nullable=False)
    external_conversation_id = db.Column(db.String(200))
    status = db.Column(
        db.String(30),
        nullable=False,
        server_default=sa.text("'open'"),
    )
    metadata_json = db.Column(
        JSONType,
        nullable=False,
        server_default=sa.text("'{}'"),
    )
    created_at = db.Column(
        db.DateTime,
        default=utcnow,
        nullable=False,
    )
    updated_at = db.Column(
        db.DateTime,
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
    )

    __table_args__ = (
        sa.Index("ix_conversations_company_channel", "company_id", "channel"),
        sa.Index("ix_conversations_company_external_id", "company_id", "external_conversation_id"),
        sa.Index("ix_conversations_company_status", "company_id", "status"),
    )


class ConversationMessage(db.Model):
    __tablename__ = "conversation_messages"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    conversation_id = db.Column(
        db.Integer,
        db.ForeignKey("conversations.id"),
        nullable=False,
    )
    company_id = db.Column(
        db.Integer,
        db.ForeignKey("companies.id"),
        nullable=False,
    )
    sender_type = db.Column(db.String(30), nullable=False)
    sender_id = db.Column(db.Integer, nullable=True)
    role = db.Column(db.String(30), nullable=True)
    content = db.Column(db.Text, nullable=False)
    content_json = db.Column(JSONType, nullable=True)
    content_type = db.Column(
        db.String(40),
        nullable=False,
        server_default=sa.text("'text'"),
    )
    external_message_id = db.Column(db.String(255), nullable=True)
    idempotency_key = db.Column(db.String(120), nullable=True)
    trace_id = db.Column(db.String(120), nullable=True)
    metadata_json = db.Column(
        JSONType,
        nullable=False,
        server_default=sa.text("'{}'"),
    )
    created_at = db.Column(
        db.DateTime,
        default=utcnow,
        nullable=False,
    )
    delivered_at = db.Column(db.DateTime, nullable=True)

    __table_args__ = (
        sa.Index(
            "uq_convmsg_external_company_conv",
            "company_id",
            "conversation_id",
            "external_message_id",
            unique=True,
            postgresql_where=sa.text("external_message_id IS NOT NULL"),
        ),
        sa.Index(
            "uq_convmsg_company_idempotency",
            "company_id",
            "idempotency_key",
            unique=True,
            postgresql_where=sa.text("idempotency_key IS NOT NULL"),
        ),
        sa.Index("ix_convmsgs_company_conversation", "company_id", "conversation_id"),
        sa.Index("ix_convmsgs_company_created", "company_id", "created_at"),
    )


class ConversationParticipant(db.Model):
    __tablename__ = "conversation_participants"

    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(
        db.Integer,
        db.ForeignKey("conversations.id"),
        nullable=False,
    )
    company_id = db.Column(
        db.Integer,
        db.ForeignKey("companies.id"),
        nullable=False,
    )
    participant_type = db.Column(db.String(40), nullable=False)
    participant_id = db.Column(db.Integer, nullable=True)
    display_name = db.Column(db.String(200), nullable=True)
    created_at = db.Column(
        db.DateTime,
        default=utcnow,
        nullable=False,
    )

    __table_args__ = (
        sa.Index("ix_convparts_company_conv", "company_id", "conversation_id"),
    )
