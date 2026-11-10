"""Add idempotent transfer batches and WhatsApp phone ownership registry.

Revision ID: 20261109_02_multibranch_integrity
Revises: 20261109_01_branch_receipts
Create Date: 2026-11-09
"""
from alembic import op
import sqlalchemy as sa


revision = "20261109_02_multibranch_integrity"
down_revision = "20261109_01_branch_receipts"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "branch_stock_transfer_batches" not in tables:
        op.create_table(
            "branch_stock_transfer_batches",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
            sa.Column("source_branch_id", sa.Integer(), sa.ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("target_branch_id", sa.Integer(), sa.ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("idempotency_key", sa.String(length=120), nullable=False),
            sa.Column("payload_hash", sa.String(length=64), nullable=False),
            sa.Column("note", sa.Text(), nullable=True),
            sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("company_id", "idempotency_key", name="uq_branch_transfer_batch_company_key"),
        )
        op.create_index("ix_branch_transfer_batches_company_created", "branch_stock_transfer_batches", ["company_id", "created_at"])
        op.create_index("ix_branch_stock_transfer_batches_company_id", "branch_stock_transfer_batches", ["company_id"])

    if "branch_stock_transfers" in tables:
        columns = {item["name"] for item in sa.inspect(bind).get_columns("branch_stock_transfers")}
        if "batch_id" not in columns:
            with op.batch_alter_table("branch_stock_transfers", recreate="auto") as batch:
                batch.add_column(sa.Column("batch_id", sa.Integer(), nullable=True))
                batch.create_foreign_key(
                    "fk_branch_stock_transfers_batch_id",
                    "branch_stock_transfer_batches",
                    ["batch_id"],
                    ["id"],
                    ondelete="CASCADE",
                )
        indexes = {item["name"] for item in sa.inspect(bind).get_indexes("branch_stock_transfers")}
        if "ix_branch_stock_transfers_batch" not in indexes:
            op.create_index("ix_branch_stock_transfers_batch", "branch_stock_transfers", ["batch_id"])

    if "whatsapp_channel_registrations" not in tables:
        op.create_table(
            "whatsapp_channel_registrations",
            sa.Column("phone_number_id", sa.String(length=40), primary_key=True),
            sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
            sa.Column("branch_id", sa.Integer(), sa.ForeignKey("branches.id", ondelete="SET NULL"), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_whatsapp_channel_registrations_company_id", "whatsapp_channel_registrations", ["company_id"])
        op.create_index("ix_whatsapp_channel_registrations_branch_id", "whatsapp_channel_registrations", ["branch_id"])


def downgrade():
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "branch_stock_transfers" in tables:
        indexes = {item["name"] for item in sa.inspect(bind).get_indexes("branch_stock_transfers")}
        if "ix_branch_stock_transfers_batch" in indexes:
            op.drop_index("ix_branch_stock_transfers_batch", table_name="branch_stock_transfers")
        columns = {item["name"] for item in sa.inspect(bind).get_columns("branch_stock_transfers")}
        if "batch_id" in columns:
            with op.batch_alter_table("branch_stock_transfers", recreate="auto") as batch:
                batch.drop_constraint("fk_branch_stock_transfers_batch_id", type_="foreignkey")
                batch.drop_column("batch_id")
    if "whatsapp_channel_registrations" in tables:
        op.drop_index("ix_whatsapp_channel_registrations_branch_id", table_name="whatsapp_channel_registrations")
        op.drop_index("ix_whatsapp_channel_registrations_company_id", table_name="whatsapp_channel_registrations")
        op.drop_table("whatsapp_channel_registrations")
    if "branch_stock_transfer_batches" in tables:
        op.drop_index("ix_branch_transfer_batches_company_id", table_name="branch_stock_transfer_batches")
        op.drop_index("ix_branch_transfer_batches_company_created", table_name="branch_stock_transfer_batches")
        op.drop_table("branch_stock_transfer_batches")
