"""Persist branch ownership for POS QR payments and their reservations."""
from alembic import op
import sqlalchemy as sa

revision = "20261103_01_pos_qr_multibranch"
down_revision = "20261102_01_multibranch_opening"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    payment_columns = {column["name"] for column in inspector.get_columns("payments")}
    if "branch_id" not in payment_columns:
        op.add_column(
            "payments",
            sa.Column("branch_id", sa.Integer(), sa.ForeignKey("branches.id", ondelete="SET NULL"), nullable=True),
        )
        op.create_index("ix_payments_branch_id", "payments", ["branch_id"])
        op.create_index("ix_payments_company_branch", "payments", ["company_id", "branch_id"])

    transfer_columns = {column["name"] for column in inspector.get_columns("branch_stock_transfers")}
    if "request_key" not in transfer_columns:
        op.add_column("branch_stock_transfers", sa.Column("request_key", sa.String(length=80), nullable=True))
        op.create_index("ix_branch_stock_transfers_request_key", "branch_stock_transfers", ["request_key"])
        op.create_index(
            "uq_branch_transfer_request_product",
            "branch_stock_transfers",
            ["company_id", "request_key", "product_id"],
            unique=True,
        )

    if not inspector.has_table("branch_stock_reservations"):
        op.create_table(
            "branch_stock_reservations",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("payment_id", sa.Integer(), sa.ForeignKey("payments.id", ondelete="CASCADE"), nullable=False),
            sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
            sa.Column("branch_id", sa.Integer(), sa.ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("quantity", sa.Numeric(18, 3), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="reserved"),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("released_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint("payment_id", "product_id", name="uq_branch_stock_reservation_payment_product"),
        )
        op.create_index("ix_branch_stock_reservations_payment_id", "branch_stock_reservations", ["payment_id"])
        op.create_index("ix_branch_stock_reservations_company_id", "branch_stock_reservations", ["company_id"])
        op.create_index("ix_branch_stock_reservations_branch_id", "branch_stock_reservations", ["branch_id"])
        op.create_index("ix_branch_stock_reservations_product_id", "branch_stock_reservations", ["product_id"])
        op.create_index("ix_branch_stock_reservations_status", "branch_stock_reservations", ["status"])
        op.create_index(
            "ix_branch_stock_reservations_company_branch_status",
            "branch_stock_reservations",
            ["company_id", "branch_id", "status"],
        )


def downgrade():
    # Preserve reservation and payment audit data on downgrade.
    pass
