"""Add settlement records for parent referral network commissions."""
from alembic import op
import sqlalchemy as sa

revision = "20260910_02_referral_network_payouts"
down_revision = "20260910_01_referral_multilevel_network"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "referral_network_payouts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("parent_seller_id", sa.Integer(), sa.ForeignKey("referral_sellers.id"), nullable=False, index=True),
        sa.Column("processed_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("transfer_date", sa.DateTime(), nullable=False),
        sa.Column("payment_method", sa.String(80)),
        sa.Column("receipt", sa.String(255)),
        sa.Column("transfer_number", sa.String(120)),
        sa.Column("observations", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now(), index=True),
    )
    op.add_column("referral_network_commissions", sa.Column("payout_id", sa.Integer(), nullable=True))
    op.create_index("ix_referral_network_commissions_payout_id", "referral_network_commissions", ["payout_id"])
    op.create_foreign_key("fk_referral_network_commissions_payout_id", "referral_network_commissions", "referral_network_payouts", ["payout_id"], ["id"])


def downgrade():
    op.drop_constraint("fk_referral_network_commissions_payout_id", "referral_network_commissions", type_="foreignkey")
    op.drop_index("ix_referral_network_commissions_payout_id", table_name="referral_network_commissions")
    op.drop_column("referral_network_commissions", "payout_id")
    op.drop_table("referral_network_payouts")
