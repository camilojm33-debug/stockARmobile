"""Persist WhatsApp consent evidence and delivery lifecycle states.

Revision ID: 20261110_01_marketing_hardening
Revises: 20261109_02_mb_integrity
Create Date: 2026-11-10
"""
from alembic import op
import sqlalchemy as sa


revision = "20261110_01_marketing_hardening"
down_revision = "20261109_02_mb_integrity"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    client_columns = {item["name"] for item in inspector.get_columns("clients")}
    with op.batch_alter_table("clients") as batch:
        if "whatsapp_marketing_consent_recorded_at" not in client_columns:
            batch.add_column(sa.Column("whatsapp_marketing_consent_recorded_at", sa.DateTime(), nullable=True))
        if "whatsapp_marketing_consent_source" not in client_columns:
            batch.add_column(sa.Column("whatsapp_marketing_consent_source", sa.String(length=40), nullable=True))
        if "whatsapp_marketing_consent_evidence" not in client_columns:
            batch.add_column(sa.Column("whatsapp_marketing_consent_evidence", sa.Text(), nullable=True))

    tables = set(inspector.get_table_names())
    if "whatsapp_consent_events" not in tables:
        op.create_table(
            "whatsapp_consent_events",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
            sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id", ondelete="CASCADE"), nullable=False),
            sa.Column("previous_status", sa.String(length=20), nullable=False),
            sa.Column("new_status", sa.String(length=20), nullable=False),
            sa.Column("source", sa.String(length=40), nullable=False),
            sa.Column("evidence", sa.Text(), nullable=False),
            sa.Column("actor_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("occurred_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_whatsapp_consent_events_company_id", "whatsapp_consent_events", ["company_id"])
        op.create_index("ix_whatsapp_consent_events_client_id", "whatsapp_consent_events", ["client_id"])

    if "ai_campaign_recipients" in tables:
        recipient_columns = {item["name"] for item in inspector.get_columns("ai_campaign_recipients")}
        with op.batch_alter_table("ai_campaign_recipients") as batch:
            if "provider_status" not in recipient_columns:
                batch.add_column(sa.Column("provider_status", sa.String(length=30), nullable=True))
            if "delivered_at" not in recipient_columns:
                batch.add_column(sa.Column("delivered_at", sa.DateTime(), nullable=True))
            if "read_at" not in recipient_columns:
                batch.add_column(sa.Column("read_at", sa.DateTime(), nullable=True))
            if "sending_at" not in recipient_columns:
                batch.add_column(sa.Column("sending_at", sa.DateTime(), nullable=True))
        indexes = {item["name"] for item in sa.inspect(bind).get_indexes("ai_campaign_recipients")}
        if "ix_ai_campaign_recipients_provider_message_id" not in indexes:
            op.create_index(
                "ix_ai_campaign_recipients_provider_message_id",
                "ai_campaign_recipients",
                ["provider_message_id"],
            )


def downgrade():
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "ai_campaign_recipients" in tables:
        indexes = {item["name"] for item in sa.inspect(bind).get_indexes("ai_campaign_recipients")}
        if "ix_ai_campaign_recipients_provider_message_id" in indexes:
            op.drop_index("ix_ai_campaign_recipients_provider_message_id", table_name="ai_campaign_recipients")
        columns = {item["name"] for item in sa.inspect(bind).get_columns("ai_campaign_recipients")}
        with op.batch_alter_table("ai_campaign_recipients") as batch:
            for name in ("sending_at", "read_at", "delivered_at", "provider_status"):
                if name in columns:
                    batch.drop_column(name)
    if "whatsapp_consent_events" in tables:
        op.drop_table("whatsapp_consent_events")
    columns = {item["name"] for item in sa.inspect(bind).get_columns("clients")}
    with op.batch_alter_table("clients") as batch:
        for name in (
            "whatsapp_marketing_consent_evidence",
            "whatsapp_marketing_consent_source",
            "whatsapp_marketing_consent_recorded_at",
        ):
            if name in columns:
                batch.drop_column(name)
