"""Create SaaS CRM center tables.

Revision ID: 20260723_01_saas_crm_center
Revises: 20260722_01_sale_modification_history
Create Date: 2026-07-23 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260723_01_saas_crm_center"
down_revision = "20260722_01_sale_modification_history"
branch_labels = None
depends_on = None


def _has_table(bind, table_name):
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def upgrade():
    bind = op.get_bind()
    if not _has_table(bind, "saas_leads"):
        op.create_table(
            "saas_leads",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_name", sa.String(length=160), nullable=False),
            sa.Column("contact_name", sa.String(length=160), nullable=False),
            sa.Column("email", sa.String(length=160), nullable=True),
            sa.Column("phone", sa.String(length=40), nullable=True),
            sa.Column("source", sa.String(length=80), nullable=False, server_default="manual"),
            sa.Column("status", sa.String(length=30), nullable=False, server_default="nuevo"),
            sa.Column("priority", sa.String(length=20), nullable=False, server_default="media"),
            sa.Column("next_follow_up_at", sa.DateTime(), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=True),
            sa.Column("assigned_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("converted_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_saas_leads_company_name", "saas_leads", ["company_name"], unique=False)
        op.create_index("ix_saas_leads_contact_name", "saas_leads", ["contact_name"], unique=False)
        op.create_index("ix_saas_leads_email", "saas_leads", ["email"], unique=False)
        op.create_index("ix_saas_leads_source", "saas_leads", ["source"], unique=False)
        op.create_index("ix_saas_leads_status", "saas_leads", ["status"], unique=False)
        op.create_index("ix_saas_leads_priority", "saas_leads", ["priority"], unique=False)
        op.create_index("ix_saas_leads_next_follow_up_at", "saas_leads", ["next_follow_up_at"], unique=False)
        op.create_index("ix_saas_leads_company_id", "saas_leads", ["company_id"], unique=False)
        op.create_index("ix_saas_leads_assigned_user_id", "saas_leads", ["assigned_user_id"], unique=False)
        op.create_index("ix_saas_leads_created_by_user_id", "saas_leads", ["created_by_user_id"], unique=False)
        op.create_index("ix_saas_leads_created_at", "saas_leads", ["created_at"], unique=False)

    if not _has_table(bind, "saas_tasks"):
        op.create_table(
            "saas_tasks",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("lead_id", sa.Integer(), sa.ForeignKey("saas_leads.id"), nullable=True),
            sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=True),
            sa.Column("title", sa.String(length=180), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("status", sa.String(length=30), nullable=False, server_default="pendiente"),
            sa.Column("priority", sa.String(length=20), nullable=False, server_default="media"),
            sa.Column("due_at", sa.DateTime(), nullable=True),
            sa.Column("completed_at", sa.DateTime(), nullable=True),
            sa.Column("assigned_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_saas_tasks_lead_id", "saas_tasks", ["lead_id"], unique=False)
        op.create_index("ix_saas_tasks_company_id", "saas_tasks", ["company_id"], unique=False)
        op.create_index("ix_saas_tasks_title", "saas_tasks", ["title"], unique=False)
        op.create_index("ix_saas_tasks_status", "saas_tasks", ["status"], unique=False)
        op.create_index("ix_saas_tasks_priority", "saas_tasks", ["priority"], unique=False)
        op.create_index("ix_saas_tasks_due_at", "saas_tasks", ["due_at"], unique=False)
        op.create_index("ix_saas_tasks_completed_at", "saas_tasks", ["completed_at"], unique=False)
        op.create_index("ix_saas_tasks_assigned_user_id", "saas_tasks", ["assigned_user_id"], unique=False)
        op.create_index("ix_saas_tasks_created_by_user_id", "saas_tasks", ["created_by_user_id"], unique=False)
        op.create_index("ix_saas_tasks_created_at", "saas_tasks", ["created_at"], unique=False)

    if not _has_table(bind, "saas_alerts"):
        op.create_table(
            "saas_alerts",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=True),
            sa.Column("lead_id", sa.Integer(), sa.ForeignKey("saas_leads.id"), nullable=True),
            sa.Column("task_id", sa.Integer(), sa.ForeignKey("saas_tasks.id"), nullable=True),
            sa.Column("title", sa.String(length=180), nullable=False),
            sa.Column("message", sa.Text(), nullable=False),
            sa.Column("category", sa.String(length=40), nullable=False, server_default="operativa"),
            sa.Column("severity", sa.String(length=20), nullable=False, server_default="media"),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="abierta"),
            sa.Column("assigned_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("acknowledged_at", sa.DateTime(), nullable=True),
            sa.Column("resolved_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_saas_alerts_company_id", "saas_alerts", ["company_id"], unique=False)
        op.create_index("ix_saas_alerts_lead_id", "saas_alerts", ["lead_id"], unique=False)
        op.create_index("ix_saas_alerts_task_id", "saas_alerts", ["task_id"], unique=False)
        op.create_index("ix_saas_alerts_title", "saas_alerts", ["title"], unique=False)
        op.create_index("ix_saas_alerts_category", "saas_alerts", ["category"], unique=False)
        op.create_index("ix_saas_alerts_severity", "saas_alerts", ["severity"], unique=False)
        op.create_index("ix_saas_alerts_status", "saas_alerts", ["status"], unique=False)
        op.create_index("ix_saas_alerts_assigned_user_id", "saas_alerts", ["assigned_user_id"], unique=False)
        op.create_index("ix_saas_alerts_created_by_user_id", "saas_alerts", ["created_by_user_id"], unique=False)
        op.create_index("ix_saas_alerts_created_at", "saas_alerts", ["created_at"], unique=False)


def downgrade():
    bind = op.get_bind()
    if _has_table(bind, "saas_alerts"):
        op.drop_index("ix_saas_alerts_created_at", table_name="saas_alerts")
        op.drop_index("ix_saas_alerts_created_by_user_id", table_name="saas_alerts")
        op.drop_index("ix_saas_alerts_assigned_user_id", table_name="saas_alerts")
        op.drop_index("ix_saas_alerts_status", table_name="saas_alerts")
        op.drop_index("ix_saas_alerts_severity", table_name="saas_alerts")
        op.drop_index("ix_saas_alerts_category", table_name="saas_alerts")
        op.drop_index("ix_saas_alerts_title", table_name="saas_alerts")
        op.drop_index("ix_saas_alerts_task_id", table_name="saas_alerts")
        op.drop_index("ix_saas_alerts_lead_id", table_name="saas_alerts")
        op.drop_index("ix_saas_alerts_company_id", table_name="saas_alerts")
        op.drop_table("saas_alerts")

    if _has_table(bind, "saas_tasks"):
        op.drop_index("ix_saas_tasks_created_at", table_name="saas_tasks")
        op.drop_index("ix_saas_tasks_created_by_user_id", table_name="saas_tasks")
        op.drop_index("ix_saas_tasks_assigned_user_id", table_name="saas_tasks")
        op.drop_index("ix_saas_tasks_completed_at", table_name="saas_tasks")
        op.drop_index("ix_saas_tasks_due_at", table_name="saas_tasks")
        op.drop_index("ix_saas_tasks_priority", table_name="saas_tasks")
        op.drop_index("ix_saas_tasks_status", table_name="saas_tasks")
        op.drop_index("ix_saas_tasks_title", table_name="saas_tasks")
        op.drop_index("ix_saas_tasks_company_id", table_name="saas_tasks")
        op.drop_index("ix_saas_tasks_lead_id", table_name="saas_tasks")
        op.drop_table("saas_tasks")

    if _has_table(bind, "saas_leads"):
        op.drop_index("ix_saas_leads_created_at", table_name="saas_leads")
        op.drop_index("ix_saas_leads_created_by_user_id", table_name="saas_leads")
        op.drop_index("ix_saas_leads_assigned_user_id", table_name="saas_leads")
        op.drop_index("ix_saas_leads_company_id", table_name="saas_leads")
        op.drop_index("ix_saas_leads_next_follow_up_at", table_name="saas_leads")
        op.drop_index("ix_saas_leads_priority", table_name="saas_leads")
        op.drop_index("ix_saas_leads_status", table_name="saas_leads")
        op.drop_index("ix_saas_leads_source", table_name="saas_leads")
        op.drop_index("ix_saas_leads_email", table_name="saas_leads")
        op.drop_index("ix_saas_leads_contact_name", table_name="saas_leads")
        op.drop_index("ix_saas_leads_company_name", table_name="saas_leads")
        op.drop_table("saas_leads")
