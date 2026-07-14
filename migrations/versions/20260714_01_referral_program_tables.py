"""referral program core tables

Revision ID: 20260714_01_referral_program
Revises: 20260713_05
Create Date: 2026-07-14 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260714_01_referral_program"
down_revision = "20260713_05"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "referral_sellers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("dni", sa.String(length=20), nullable=False),
        sa.Column("tax_id", sa.String(length=32), nullable=True),
        sa.Column("phone", sa.String(length=32), nullable=True),
        sa.Column("province", sa.String(length=64), nullable=True),
        sa.Column("city", sa.String(length=64), nullable=True),
        sa.Column("address", sa.String(length=256), nullable=True),
        sa.Column("alias", sa.String(length=64), nullable=True),
        sa.Column("cbu", sa.String(length=22), nullable=True),
        sa.Column("bank", sa.String(length=80), nullable=True),
        sa.Column("account_holder", sa.String(length=120), nullable=True),
        sa.Column("referral_code", sa.String(length=24), nullable=False),
        sa.Column("referral_url", sa.String(length=255), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dni", name="uq_referral_sellers_dni"),
        sa.UniqueConstraint("referral_code", name="uq_referral_sellers_code"),
        sa.UniqueConstraint("user_id", name="uq_referral_sellers_user"),
    )

    op.create_table(
        "referral_attributions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("seller_id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("referral_code", sa.String(length=24), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["seller_id"], ["referral_sellers.id"]),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", name="uq_referral_attributions_company"),
    )

    op.create_table(
        "referral_commissions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("seller_id", sa.Integer(), nullable=False),
        sa.Column("attribution_id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("subscription_id", sa.Integer(), nullable=True),
        sa.Column("payment_id", sa.Integer(), nullable=True),
        sa.Column("plan_id", sa.Integer(), nullable=True),
        sa.Column("sold_amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("commission_percent", sa.Numeric(6, 4), nullable=False, server_default="0.3000"),
        sa.Column("commission_amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pendiente"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("available_at", sa.DateTime(), nullable=True),
        sa.Column("paid_at", sa.DateTime(), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["seller_id"], ["referral_sellers.id"]),
        sa.ForeignKeyConstraint(["attribution_id"], ["referral_attributions.id"]),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.ForeignKeyConstraint(["subscription_id"], ["subscriptions.id"]),
        sa.ForeignKeyConstraint(["payment_id"], ["payments.id"]),
        sa.ForeignKeyConstraint(["plan_id"], ["plans.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_referral_commissions_seller_status", "referral_commissions", ["seller_id", "status"])
    op.create_index("ix_referral_commissions_company", "referral_commissions", ["company_id"])
    op.create_index("ix_referral_commissions_available", "referral_commissions", ["available_at"])

    op.create_table(
        "referral_payouts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("seller_id", sa.Integer(), nullable=False),
        sa.Column("processed_by_user_id", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("transfer_date", sa.DateTime(), nullable=False),
        sa.Column("receipt", sa.String(length=255), nullable=True),
        sa.Column("transfer_number", sa.String(length=80), nullable=True),
        sa.Column("observations", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["seller_id"], ["referral_sellers.id"]),
        sa.ForeignKeyConstraint(["processed_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "referral_payout_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("payout_id", sa.Integer(), nullable=False),
        sa.Column("commission_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["payout_id"], ["referral_payouts.id"]),
        sa.ForeignKeyConstraint(["commission_id"], ["referral_commissions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("commission_id", name="uq_referral_payout_items_commission"),
    )


def downgrade():
    op.drop_table("referral_payout_items")
    op.drop_table("referral_payouts")
    op.drop_index("ix_referral_commissions_available", table_name="referral_commissions")
    op.drop_index("ix_referral_commissions_company", table_name="referral_commissions")
    op.drop_index("ix_referral_commissions_seller_status", table_name="referral_commissions")
    op.drop_table("referral_commissions")
    op.drop_table("referral_attributions")
    op.drop_table("referral_sellers")
