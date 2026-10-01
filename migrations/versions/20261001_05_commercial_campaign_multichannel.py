"""Extend SuperAdmin commercial campaigns for multichannel delivery and tracking."""

from alembic import op
import sqlalchemy as sa

revision = "20261001_05_commercial_campaign_multichannel"
down_revision = "20261001_04_commercial_activation_secret"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint(
        "saas_commercial_checkouts_lead_id_fkey",
        "saas_commercial_checkouts",
        type_="foreignkey",
    )
    op.alter_column(
        "saas_commercial_checkouts",
        "lead_id",
        existing_type=sa.Integer(),
        nullable=True,
    )
    op.create_foreign_key(
        "saas_commercial_checkouts_lead_id_fkey",
        "saas_commercial_checkouts",
        "saas_leads",
        ["lead_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column("saas_campaigns", sa.Column("whatsapp_template_name", sa.String(length=120)))
    op.add_column("saas_campaigns", sa.Column("whatsapp_template_language", sa.String(length=20), server_default="es_AR"))
    op.add_column("saas_campaigns", sa.Column("whatsapp_parameter_fields", sa.Text(), server_default="contacto,empresa"))
    op.add_column("saas_campaigns", sa.Column("replied_count", sa.Integer(), nullable=False, server_default="0"))

    op.add_column("saas_campaign_recipients", sa.Column("tracking_token", sa.String(length=96)))
    op.add_column("saas_campaign_recipients", sa.Column("provider_status", sa.String(length=40)))
    op.create_index(
        "ix_saas_campaign_recipients_tracking_token",
        "saas_campaign_recipients",
        ["tracking_token"],
        unique=True,
    )
    op.create_index(
        "ix_saas_campaign_recipients_provider_message_id",
        "saas_campaign_recipients",
        ["provider_message_id"],
        unique=False,
    )


def downgrade():
    op.drop_constraint(
        "saas_commercial_checkouts_lead_id_fkey",
        "saas_commercial_checkouts",
        type_="foreignkey",
    )
    op.alter_column(
        "saas_commercial_checkouts",
        "lead_id",
        existing_type=sa.Integer(),
        nullable=False,
    )
    op.create_foreign_key(
        "saas_commercial_checkouts_lead_id_fkey",
        "saas_commercial_checkouts",
        "saas_leads",
        ["lead_id"],
        ["id"],
    )
    op.drop_index("ix_saas_campaign_recipients_provider_message_id", table_name="saas_campaign_recipients")
    op.drop_index("ix_saas_campaign_recipients_tracking_token", table_name="saas_campaign_recipients")
    op.drop_column("saas_campaign_recipients", "provider_status")
    op.drop_column("saas_campaign_recipients", "tracking_token")
    op.drop_column("saas_campaigns", "replied_count")
    op.drop_column("saas_campaigns", "whatsapp_parameter_fields")
    op.drop_column("saas_campaigns", "whatsapp_template_language")
    op.drop_column("saas_campaigns", "whatsapp_template_name")
