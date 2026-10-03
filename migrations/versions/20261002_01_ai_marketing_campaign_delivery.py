"""Add tenant AI campaign delivery, consent and attribution."""
from alembic import op
import sqlalchemy as sa

revision = "20261002_01_ai_marketing_campaign_delivery"
down_revision = "20261001_06_saas_lead_delete"
branch_labels = None
depends_on = None

def upgrade():
    with op.batch_alter_table("clients") as batch_op:
        batch_op.add_column(sa.Column("email_marketing_consent", sa.String(20), nullable=False, server_default="unknown"))
        batch_op.add_column(sa.Column("whatsapp_marketing_consent", sa.String(20), nullable=False, server_default="unknown"))
        batch_op.add_column(sa.Column("marketing_unsubscribe_token", sa.String(120), nullable=True))
        batch_op.create_index("ix_clients_email_marketing_consent", ["email_marketing_consent"])
        batch_op.create_index("ix_clients_whatsapp_marketing_consent", ["whatsapp_marketing_consent"])
        batch_op.create_index("uq_clients_marketing_unsubscribe_token", ["marketing_unsubscribe_token"], unique=True)

    with op.batch_alter_table("ai_campaigns") as batch_op:
        batch_op.add_column(sa.Column("channel", sa.String(20), nullable=False, server_default="email"))
        batch_op.add_column(sa.Column("scheduled_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("target_count", sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("sent_count", sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("skipped_count", sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("started_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("finished_at", sa.DateTime(), nullable=True))
        batch_op.create_index("ix_ai_campaigns_company_scheduled", ["company_id", "scheduled_at"])

    op.create_table(
        "ai_campaign_recipients",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("client_id", sa.Integer(), nullable=False),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("destination", sa.String(255), nullable=False),
        sa.Column("unsubscribe_token", sa.String(120), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("provider_message_id", sa.String(255), nullable=True),
        sa.Column("error_reason", sa.String(2000), nullable=True),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.Column("replied_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["campaign_id"], ["ai_campaigns.id"]),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"]),
        sa.UniqueConstraint("campaign_id", "client_id", "channel", name="uq_ai_campaign_recipient_target"),
    )
    op.create_index("ix_ai_campaign_recipients_campaign_status", "ai_campaign_recipients", ["campaign_id", "status"])
    op.create_index("ix_ai_campaign_recipients_company_client", "ai_campaign_recipients", ["company_id", "client_id"])
    op.create_index("ix_ai_campaign_recipients_destination", "ai_campaign_recipients", ["company_id", "destination"])

    op.create_table(
        "ai_campaign_attributions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("client_id", sa.Integer(), nullable=False),
        sa.Column("recipient_id", sa.Integer(), nullable=True),
        sa.Column("conversation_id", sa.Integer(), nullable=True),
        sa.Column("sale_id", sa.Integer(), nullable=True),
        sa.Column("revenue", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("attribution_type", sa.String(30), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["campaign_id"], ["ai_campaigns.id"]),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"]),
        sa.ForeignKeyConstraint(["recipient_id"], ["ai_campaign_recipients.id"]),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"]),
        sa.ForeignKeyConstraint(["sale_id"], ["sales.id"]),
        sa.UniqueConstraint("campaign_id", "sale_id", name="uq_ai_campaign_sale_attribution"),
    )
    op.create_index("ix_ai_campaign_attr_company_campaign", "ai_campaign_attributions", ["company_id", "campaign_id"])
    op.create_index("ix_ai_campaign_attr_company_client", "ai_campaign_attributions", ["company_id", "client_id"])

def downgrade():
    op.drop_table("ai_campaign_attributions")
    op.drop_table("ai_campaign_recipients")
    with op.batch_alter_table("ai_campaigns") as batch_op:
        batch_op.drop_index("ix_ai_campaigns_company_scheduled")
        for column in ("finished_at", "started_at", "skipped_count", "failed_count", "sent_count", "target_count", "scheduled_at", "channel"):
            batch_op.drop_column(column)
    with op.batch_alter_table("clients") as batch_op:
        batch_op.drop_index("uq_clients_marketing_unsubscribe_token")
        batch_op.drop_index("ix_clients_whatsapp_marketing_consent")
        batch_op.drop_index("ix_clients_email_marketing_consent")
        batch_op.drop_column("marketing_unsubscribe_token")
        batch_op.drop_column("whatsapp_marketing_consent")
        batch_op.drop_column("email_marketing_consent")
