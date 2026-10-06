"""Create isolated tenant CRM opportunities and activities.

This migration belongs to the tenant CRM only. It does not touch the
SuperAdmin SaaS CRM tables (saas_leads, saas_tasks, saas_alerts, etc.).
"""
from alembic import op
import sqlalchemy as sa

revision = "20261006_01_tenant_crm"
down_revision = "20261005_02_saas_lead_suppression"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "crm_opportunities",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(180), nullable=False),
        sa.Column("stage", sa.String(30), nullable=False, server_default="nuevo"),
        sa.Column("status", sa.String(20), nullable=False, server_default="open"),
        sa.Column("value", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("probability", sa.Numeric(5, 2), nullable=False, server_default="0"),
        sa.Column("expected_close_date", sa.DateTime(), nullable=True),
        sa.Column("owner_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("source", sa.String(60), nullable=True),
        sa.Column("quote_id", sa.Integer(), sa.ForeignKey("quotes.id", ondelete="SET NULL"), nullable=True),
        sa.Column("sale_id", sa.Integer(), sa.ForeignKey("sales.id", ondelete="SET NULL"), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_crm_opportunities_company_stage_status", "crm_opportunities", ["company_id", "stage", "status"])
    op.create_index("ix_crm_opportunities_company_client", "crm_opportunities", ["company_id", "client_id"])
    op.create_index("ix_crm_opportunities_company_owner", "crm_opportunities", ["company_id", "owner_user_id"])
    op.create_index("ix_crm_opportunities_company_updated", "crm_opportunities", ["company_id", "updated_at"])

    op.create_table(
        "crm_activities",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("opportunity_id", sa.Integer(), sa.ForeignKey("crm_opportunities.id", ondelete="CASCADE"), nullable=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("type", sa.String(30), nullable=False, server_default="tarea"),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("subject", sa.String(180), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("due_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_crm_activities_company_status_due", "crm_activities", ["company_id", "status", "due_at"])
    op.create_index("ix_crm_activities_company_client", "crm_activities", ["company_id", "client_id"])
    op.create_index("ix_crm_activities_company_opportunity", "crm_activities", ["company_id", "opportunity_id"])


def downgrade():
    op.drop_index("ix_crm_activities_company_opportunity", table_name="crm_activities")
    op.drop_index("ix_crm_activities_company_client", table_name="crm_activities")
    op.drop_index("ix_crm_activities_company_status_due", table_name="crm_activities")
    op.drop_table("crm_activities")
    op.drop_index("ix_crm_opportunities_company_updated", table_name="crm_opportunities")
    op.drop_index("ix_crm_opportunities_company_owner", table_name="crm_opportunities")
    op.drop_index("ix_crm_opportunities_company_client", table_name="crm_opportunities")
    op.drop_index("ix_crm_opportunities_company_stage_status", table_name="crm_opportunities")
    op.drop_table("crm_opportunities")
