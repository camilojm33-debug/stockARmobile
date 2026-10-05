"""Create permanent CRM lead suppression records."""
from datetime import datetime
from alembic import op
import sqlalchemy as sa

revision = "20261005_02_saas_lead_suppression"
down_revision = "20261005_01_saas_consent_backfill"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "saas_lead_suppressions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email", sa.String(length=160), nullable=True),
        sa.Column("phone", sa.String(length=40), nullable=True),
        sa.Column("company_name", sa.String(length=160), nullable=True),
        sa.Column("reason", sa.String(length=500), nullable=False, server_default="superadmin_delete"),
        sa.Column("source", sa.String(length=40), nullable=False, server_default="superadmin_delete"),
        sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_saas_lead_suppressions_email", "saas_lead_suppressions", ["email"])
    op.create_index("ix_saas_lead_suppressions_phone", "saas_lead_suppressions", ["phone"])
    op.create_index("ix_saas_lead_suppressions_company_name", "saas_lead_suppressions", ["company_name"])
    op.create_index("ix_saas_lead_suppressions_created_by_user_id", "saas_lead_suppressions", ["created_by_user_id"])
    op.create_index("ix_saas_lead_suppressions_source", "saas_lead_suppressions", ["source"])
    op.alter_column("saas_lead_suppressions", "reason", server_default=None)
    op.alter_column("saas_lead_suppressions", "source", server_default=None)


def downgrade():
    op.drop_index("ix_saas_lead_suppressions_source", table_name="saas_lead_suppressions")
    op.drop_index("ix_saas_lead_suppressions_created_by_user_id", table_name="saas_lead_suppressions")
    op.drop_index("ix_saas_lead_suppressions_company_name", table_name="saas_lead_suppressions")
    op.drop_index("ix_saas_lead_suppressions_phone", table_name="saas_lead_suppressions")
    op.drop_index("ix_saas_lead_suppressions_email", table_name="saas_lead_suppressions")
    op.drop_table("saas_lead_suppressions")
