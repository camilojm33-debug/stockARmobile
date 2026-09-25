"""Enforce AI agent/config uniqueness per tenant.

Revision ID: 20260925_01_ai_agent_uniqueness
Revises: 20260924_02_notification_individual_reads
"""
from alembic import op

revision = "20260925_01_ai_agent_uniqueness"
down_revision = "20260924_02_notification_individual_reads"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("uq_agents_company_name", "agents", ["company_id", "name"], unique=True)
    op.create_index("uq_agentcfg_company_agent", "agent_configurations", ["company_id", "agent_id"], unique=True)


def downgrade():
    op.drop_index("uq_agentcfg_company_agent", table_name="agent_configurations")
    op.drop_index("uq_agents_company_name", table_name="agents")
