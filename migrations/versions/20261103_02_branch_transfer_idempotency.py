"""Add idempotency keys to multi-product branch stock transfers."""
from alembic import op
import sqlalchemy as sa

revision = "20261103_02_branch_transfer_idempotency"
down_revision = "20261103_01_payment_branch_scope"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("branch_stock_transfers"):
        return
    columns = {column["name"] for column in inspector.get_columns("branch_stock_transfers")}
    if "operation_key" not in columns:
        op.add_column(
            "branch_stock_transfers",
            sa.Column("operation_key", sa.String(length=80), nullable=True),
        )
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("branch_stock_transfers")}
    if "ix_branch_stock_transfers_operation_key" not in indexes:
        op.create_index(
            "ix_branch_stock_transfers_operation_key",
            "branch_stock_transfers",
            ["operation_key"],
        )
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("branch_stock_transfers")}
    if "uq_branch_transfer_operation_product" not in indexes:
        op.create_index(
            "uq_branch_transfer_operation_product",
            "branch_stock_transfers",
            ["company_id", "operation_key", "product_id"],
            unique=True,
        )


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("branch_stock_transfers"):
        return
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("branch_stock_transfers")}
    if "uq_branch_transfer_operation_product" in indexes:
        op.drop_index(
            "uq_branch_transfer_operation_product",
            table_name="branch_stock_transfers",
        )
    if "ix_branch_stock_transfers_operation_key" in indexes:
        op.drop_index("ix_branch_stock_transfers_operation_key", table_name="branch_stock_transfers")
    columns = {column["name"] for column in sa.inspect(bind).get_columns("branch_stock_transfers")}
    if "operation_key" in columns:
        op.drop_column("branch_stock_transfers", "operation_key")
