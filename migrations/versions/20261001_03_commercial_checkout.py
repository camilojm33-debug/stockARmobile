"""Persist commercial checkout state for Comercial IA -> Mercado Pago -> tenant activation."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20261001_03_commercial_checkout"
down_revision = "20261001_02_finalize_current_whatsapp_move"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "saas_commercial_checkouts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("lead_id", sa.Integer(), sa.ForeignKey("saas_leads.id"), nullable=False),
        sa.Column("plan_id", sa.Integer(), sa.ForeignKey("plans.id"), nullable=False),
        sa.Column("plan_code", sa.String(length=40), nullable=False),
        sa.Column("company_name", sa.String(length=160), nullable=False),
        sa.Column("payer_email", sa.String(length=160), nullable=False),
        sa.Column("phone", sa.String(length=40), nullable=False),
        sa.Column("preapproval_id", sa.String(length=120)),
        sa.Column("external_reference", sa.String(length=255), nullable=False),
        sa.Column("checkout_url", sa.Text()),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="pending"),
        sa.Column("activation_token_hash", sa.String(length=64)),
        sa.Column("activated_at", sa.DateTime()),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id")),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime()),
        sa.UniqueConstraint("preapproval_id", name="uq_saas_commercial_checkout_preapproval"),
        sa.UniqueConstraint("external_reference", name="uq_saas_commercial_checkout_external_reference"),
        sa.UniqueConstraint("activation_token_hash", name="uq_saas_commercial_checkout_activation"),
    )
    op.create_index("ix_saas_commercial_checkouts_lead_id", "saas_commercial_checkouts", ["lead_id"])
    op.create_index("ix_saas_commercial_checkouts_plan_id", "saas_commercial_checkouts", ["plan_id"])
    op.create_index("ix_saas_commercial_checkouts_plan_code", "saas_commercial_checkouts", ["plan_code"])
    op.create_index("ix_saas_commercial_checkouts_payer_email", "saas_commercial_checkouts", ["payer_email"])
    op.create_index("ix_saas_commercial_checkouts_preapproval_id", "saas_commercial_checkouts", ["preapproval_id"])
    op.create_index("ix_saas_commercial_checkouts_status", "saas_commercial_checkouts", ["status"])
    op.create_index("ix_saas_commercial_checkouts_activation_token_hash", "saas_commercial_checkouts", ["activation_token_hash"])
    op.create_index("ix_saas_commercial_checkouts_company_id", "saas_commercial_checkouts", ["company_id"])
    op.create_index("ix_saas_commercial_checkouts_user_id", "saas_commercial_checkouts", ["user_id"])
    op.create_index("ix_saas_commercial_checkouts_created_at", "saas_commercial_checkouts", ["created_at"])


def downgrade():
    op.drop_index("ix_saas_commercial_checkouts_created_at", table_name="saas_commercial_checkouts")
    op.drop_index("ix_saas_commercial_checkouts_user_id", table_name="saas_commercial_checkouts")
    op.drop_index("ix_saas_commercial_checkouts_company_id", table_name="saas_commercial_checkouts")
    op.drop_index("ix_saas_commercial_checkouts_activation_token_hash", table_name="saas_commercial_checkouts")
    op.drop_index("ix_saas_commercial_checkouts_status", table_name="saas_commercial_checkouts")
    op.drop_index("ix_saas_commercial_checkouts_preapproval_id", table_name="saas_commercial_checkouts")
    op.drop_index("ix_saas_commercial_checkouts_payer_email", table_name="saas_commercial_checkouts")
    op.drop_index("ix_saas_commercial_checkouts_plan_code", table_name="saas_commercial_checkouts")
    op.drop_index("ix_saas_commercial_checkouts_plan_id", table_name="saas_commercial_checkouts")
    op.drop_index("ix_saas_commercial_checkouts_lead_id", table_name="saas_commercial_checkouts")
    op.drop_table("saas_commercial_checkouts")
