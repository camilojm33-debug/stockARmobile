"""Add multilevel referral parent/child network.

Revision ID: 20260910_01_referral_multilevel_network
Revises: 20260909_01_supplier_purchase_payments
"""
from alembic import op
import sqlalchemy as sa

revision = "20260910_01_referral_multilevel_network"
down_revision = "20260909_01_supplier_purchase_payments"
branch_labels = None
depends_on = None

def upgrade():
    op.create_table(
        "referral_network_links",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("parent_seller_id", sa.Integer(), sa.ForeignKey("referral_sellers.id"), nullable=False),
        sa.Column("child_seller_id", sa.Integer(), sa.ForeignKey("referral_sellers.id"), nullable=False),
        sa.Column("override_percent", sa.Numeric(6, 4), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_referral_network_links_parent", "referral_network_links", ["parent_seller_id"])
    op.create_index("ix_referral_network_links_child", "referral_network_links", ["child_seller_id"], unique=True)
    op.create_index("ix_referral_network_links_active", "referral_network_links", ["active"])

    op.create_table(
        "referral_network_commissions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("parent_seller_id", sa.Integer(), sa.ForeignKey("referral_sellers.id"), nullable=False),
        sa.Column("child_seller_id", sa.Integer(), sa.ForeignKey("referral_sellers.id"), nullable=False),
        sa.Column("source_commission_id", sa.Integer(), sa.ForeignKey("referral_commissions.id"), nullable=False),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("payment_id", sa.Integer(), sa.ForeignKey("payments.id"), nullable=True),
        sa.Column("subscription_id", sa.Integer(), sa.ForeignKey("subscriptions.id"), nullable=True),
        sa.Column("sold_amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("commission_percent", sa.Numeric(6, 4), nullable=False, server_default="0.3000"),
        sa.Column("commission_amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pendiente"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("paid_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_referral_network_commissions_parent", "referral_network_commissions", ["parent_seller_id"])
    op.create_index("ix_referral_network_commissions_child", "referral_network_commissions", ["child_seller_id"])
    op.create_index("ix_referral_network_commissions_source", "referral_network_commissions", ["source_commission_id"], unique=True)
    op.create_index("ix_referral_network_commissions_company", "referral_network_commissions", ["company_id"])
    op.create_index("ix_referral_network_commissions_payment", "referral_network_commissions", ["payment_id"])
    op.create_index("ix_referral_network_commissions_subscription", "referral_network_commissions", ["subscription_id"])
    op.create_index("ix_referral_network_commissions_status", "referral_network_commissions", ["status"])
    op.create_index("ix_referral_network_commissions_created", "referral_network_commissions", ["created_at"])

def downgrade():
    op.drop_index("ix_referral_network_commissions_created", table_name="referral_network_commissions")
    op.drop_index("ix_referral_network_commissions_status", table_name="referral_network_commissions")
    op.drop_index("ix_referral_network_commissions_subscription", table_name="referral_network_commissions")
    op.drop_index("ix_referral_network_commissions_payment", table_name="referral_network_commissions")
    op.drop_index("ix_referral_network_commissions_company", table_name="referral_network_commissions")
    op.drop_index("ix_referral_network_commissions_source", table_name="referral_network_commissions")
    op.drop_index("ix_referral_network_commissions_child", table_name="referral_network_commissions")
    op.drop_index("ix_referral_network_commissions_parent", table_name="referral_network_commissions")
    op.drop_table("referral_network_commissions")
    op.drop_index("ix_referral_network_links_active", table_name="referral_network_links")
    op.drop_index("ix_referral_network_links_child", table_name="referral_network_links")
    op.drop_index("ix_referral_network_links_parent", table_name="referral_network_links")
    op.drop_table("referral_network_links")
