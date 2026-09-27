"""Add SuperAdmin commercial prospecting CRM and controlled campaigns."""
from alembic import op
import sqlalchemy as sa

revision = "20260927_01_saas_commercial_crm"
down_revision = "20260925_01_ai_agent_uniqueness"
branch_labels = None
depends_on = None


def upgrade():
    columns = [
        ("whatsapp", sa.String(40), None), ("industry", sa.String(100), None), ("subindustry", sa.String(100), None),
        ("province", sa.String(100), None), ("locality", sa.String(120), None), ("address", sa.String(255), None),
        ("website", sa.String(255), None), ("instagram", sa.String(255), None), ("facebook", sa.String(255), None),
        ("source_url", sa.String(500), None), ("segment", sa.String(100), None),
        ("lead_score", sa.Integer(), "0"), ("email_status", sa.String(20), "'unknown'"),
        ("phone_status", sa.String(20), "'unknown'"), ("email_consent_status", sa.String(20), "'unknown'"),
        ("whatsapp_consent_status", sa.String(20), "'unknown'"), ("phone_consent_status", sa.String(20), "'unknown'"),
        ("do_not_contact", sa.Boolean(), "false"), ("do_not_contact_at", sa.DateTime(), None),
        ("captured_at", sa.DateTime(), None), ("validated_at", sa.DateTime(), None), ("last_contacted_at", sa.DateTime(), None),
        ("last_contact_channel", sa.String(20), None), ("contact_count", sa.Integer(), "0"),
    ]
    for name, typ, default in columns:
        kwargs = {"nullable": default is None}
        if default is not None:
            kwargs["nullable"] = False
            kwargs["server_default"] = sa.text(default)
        op.add_column("saas_leads", sa.Column(name, typ, **kwargs))

    lead_indexes = [
        ("ix_saas_leads_whatsapp", "whatsapp"), ("ix_saas_leads_industry", "industry"),
        ("ix_saas_leads_province", "province"), ("ix_saas_leads_locality", "locality"),
        ("ix_saas_leads_segment", "segment"), ("ix_saas_leads_lead_score", "lead_score"),
        ("ix_saas_leads_email_status", "email_status"), ("ix_saas_leads_email_consent_status", "email_consent_status"),
        ("ix_saas_leads_whatsapp_consent_status", "whatsapp_consent_status"),
        ("ix_saas_leads_do_not_contact", "do_not_contact"), ("ix_saas_leads_last_contacted_at", "last_contacted_at"),
    ]
    for name, column in lead_indexes:
        op.create_index(name, "saas_leads", [column], unique=False)

    op.create_table(
        "saas_lead_consents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("lead_id", sa.Integer(), nullable=False),
        sa.Column("email_status", sa.String(20), nullable=False, server_default="unknown"),
        sa.Column("whatsapp_status", sa.String(20), nullable=False, server_default="unknown"),
        sa.Column("phone_status", sa.String(20), nullable=False, server_default="unknown"),
        sa.Column("email_source", sa.String(80)), sa.Column("whatsapp_source", sa.String(80)),
        sa.Column("granted_at", sa.DateTime()), sa.Column("revoked_at", sa.DateTime()),
        sa.Column("unsubscribe_token", sa.String(96)), sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["lead_id"], ["saas_leads.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("lead_id"), sa.UniqueConstraint("unsubscribe_token"),
    )
    for name, column in [
        ("ix_saas_lead_consents_lead_id", "lead_id"), ("ix_saas_lead_consents_email_status", "email_status"),
        ("ix_saas_lead_consents_whatsapp_status", "whatsapp_status"), ("ix_saas_lead_consents_phone_status", "phone_status"),
        ("ix_saas_lead_consents_unsubscribe_token", "unsubscribe_token"),
    ]:
        op.create_index(name, "saas_lead_consents", [column], unique=False)

    op.create_table(
        "saas_lead_imports",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("file_name", sa.String(255), nullable=False),
        sa.Column("file_format", sa.String(20), nullable=False), sa.Column("source", sa.String(80), nullable=False, server_default="import"),
        sa.Column("status", sa.String(30), nullable=False, server_default="completed"), sa.Column("rows_read", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("inserted_count", sa.Integer(), nullable=False, server_default="0"), sa.Column("updated_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duplicate_count", sa.Integer(), nullable=False, server_default="0"), sa.Column("invalid_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_summary", sa.Text()), sa.Column("created_by_user_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False), sa.Column("completed_at", sa.DateTime()),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
    )
    for name, column in [
        ("ix_saas_lead_imports_status", "status"), ("ix_saas_lead_imports_created_by_user_id", "created_by_user_id"),
        ("ix_saas_lead_imports_created_at", "created_at"),
    ]:
        op.create_index(name, "saas_lead_imports", [column], unique=False)

    op.create_table(
        "saas_campaigns",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("name", sa.String(180), nullable=False),
        sa.Column("subject", sa.String(255), nullable=False), sa.Column("channel", sa.String(20), nullable=False, server_default="email"),
        sa.Column("status", sa.String(30), nullable=False, server_default="BORRADOR"), sa.Column("body_html", sa.Text(), nullable=False),
        sa.Column("body_text", sa.Text()), sa.Column("segment_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("scheduled_at", sa.DateTime()), sa.Column("started_at", sa.DateTime()), sa.Column("finished_at", sa.DateTime()),
        sa.Column("target_count", sa.Integer(), nullable=False, server_default="0"), sa.Column("sent_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"), sa.Column("skipped_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_by_user_id", sa.Integer(), nullable=False), sa.Column("approved_by_user_id", sa.Integer()),
        sa.Column("approved_at", sa.DateTime()), sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False), sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["approved_by_user_id"], ["users.id"]),
    )
    for name, columns in [
        ("ix_saas_campaigns_status", ["status"]), ("ix_saas_campaigns_channel", ["channel"]),
        ("ix_saas_campaigns_status_scheduled", ["status", "scheduled_at"]), ("ix_saas_campaigns_created_at", ["created_at"]),
    ]:
        op.create_index(name, "saas_campaigns", columns, unique=False)

    op.create_table(
        "saas_campaign_recipients",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.Column("lead_id", sa.Integer(), nullable=False), sa.Column("channel", sa.String(20), nullable=False, server_default="email"),
        sa.Column("destination", sa.String(255), nullable=False), sa.Column("status", sa.String(30), nullable=False, server_default="pending"),
        sa.Column("provider_message_id", sa.String(255)), sa.Column("error_reason", sa.Text()),
        sa.Column("sent_at", sa.DateTime()), sa.Column("delivered_at", sa.DateTime()), sa.Column("opened_at", sa.DateTime()),
        sa.Column("clicked_at", sa.DateTime()), sa.Column("replied_at", sa.DateTime()), sa.Column("failed_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False), sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["campaign_id"], ["saas_campaigns.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["lead_id"], ["saas_leads.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("campaign_id", "lead_id", "channel", name="uq_saas_campaign_recipient"),
    )
    for name, column in [
        ("ix_saas_campaign_recipients_campaign_id", "campaign_id"), ("ix_saas_campaign_recipients_lead_id", "lead_id"),
        ("ix_saas_campaign_recipients_status", "status"), ("ix_saas_campaign_recipients_created_at", "created_at"),
    ]:
        op.create_index(name, "saas_campaign_recipients", [column], unique=False)

    op.create_table(
        "saas_campaign_events",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.Column("recipient_id", sa.Integer()), sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column("metadata_json", sa.Text(), nullable=False, server_default="{}"), sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["campaign_id"], ["saas_campaigns.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["recipient_id"], ["saas_campaign_recipients.id"], ondelete="SET NULL"),
    )
    for name, column in [
        ("ix_saas_campaign_events_campaign_id", "campaign_id"), ("ix_saas_campaign_events_recipient_id", "recipient_id"),
        ("ix_saas_campaign_events_event_type", "event_type"), ("ix_saas_campaign_events_created_at", "created_at"),
    ]:
        op.create_index(name, "saas_campaign_events", [column], unique=False)

    op.create_table(
        "saas_message_templates",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("name", sa.String(120), nullable=False),
        sa.Column("channel", sa.String(20), nullable=False, server_default="email"), sa.Column("subject", sa.String(255)),
        sa.Column("body_html", sa.Text(), nullable=False), sa.Column("body_text", sa.Text()),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()), sa.Column("created_by_user_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False), sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
        sa.UniqueConstraint("name", "channel", name="uq_saas_message_template"),
    )
    op.create_index("ix_saas_message_templates_active", "saas_message_templates", ["active"], unique=False)
    op.create_index("ix_saas_message_templates_channel", "saas_message_templates", ["channel"], unique=False)

    op.create_table(
        "saas_lead_sequences",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("name", sa.String(160), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"), sa.Column("steps_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("created_by_user_id", sa.Integer(), nullable=False), sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False), sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
    )
    op.create_index("ix_saas_lead_sequences_status", "saas_lead_sequences", ["status"], unique=False)


def downgrade():
    op.drop_index("ix_saas_lead_sequences_status", table_name="saas_lead_sequences")
    op.drop_table("saas_lead_sequences")
    op.drop_index("ix_saas_message_templates_channel", table_name="saas_message_templates")
    op.drop_index("ix_saas_message_templates_active", table_name="saas_message_templates")
    op.drop_table("saas_message_templates")
    for name in ["ix_saas_campaign_events_created_at","ix_saas_campaign_events_event_type","ix_saas_campaign_events_recipient_id","ix_saas_campaign_events_campaign_id"]:
        op.drop_index(name, table_name="saas_campaign_events")
    op.drop_table("saas_campaign_events")
    for name in ["ix_saas_campaign_recipients_created_at","ix_saas_campaign_recipients_status","ix_saas_campaign_recipients_lead_id","ix_saas_campaign_recipients_campaign_id"]:
        op.drop_index(name, table_name="saas_campaign_recipients")
    op.drop_table("saas_campaign_recipients")
    for name in ["ix_saas_campaigns_created_at","ix_saas_campaigns_status_scheduled","ix_saas_campaigns_channel","ix_saas_campaigns_status"]:
        op.drop_index(name, table_name="saas_campaigns")
    op.drop_table("saas_campaigns")
    for name in ["ix_saas_lead_imports_created_at","ix_saas_lead_imports_created_by_user_id","ix_saas_lead_imports_status"]:
        op.drop_index(name, table_name="saas_lead_imports")
    op.drop_table("saas_lead_imports")
    for name in ["ix_saas_lead_consents_unsubscribe_token","ix_saas_lead_consents_phone_status","ix_saas_lead_consents_whatsapp_status","ix_saas_lead_consents_email_status","ix_saas_lead_consents_lead_id"]:
        op.drop_index(name, table_name="saas_lead_consents")
    op.drop_table("saas_lead_consents")
    for name in ["ix_saas_leads_last_contacted_at","ix_saas_leads_do_not_contact","ix_saas_leads_whatsapp_consent_status","ix_saas_leads_email_consent_status","ix_saas_leads_email_status","ix_saas_leads_lead_score","ix_saas_leads_segment","ix_saas_leads_locality","ix_saas_leads_province","ix_saas_leads_industry","ix_saas_leads_whatsapp"]:
        op.drop_index(name, table_name="saas_leads")
    for col in ["contact_count","last_contact_channel","last_contacted_at","validated_at","captured_at","do_not_contact_at","do_not_contact","phone_consent_status","whatsapp_consent_status","email_consent_status","phone_status","email_status","lead_score","segment","source_url","facebook","instagram","website","address","locality","province","subindustry","industry","whatsapp"]:
        op.drop_column("saas_leads", col)
