"""Add branch-specific Vendedor IA configuration and licenses.

Revision ID: 20261106_01_branch_vendor
Revises: 20261105_01_vendor_branch
Create Date: 2026-11-06
"""
from alembic import op
import sqlalchemy as sa


revision = "20261106_01_branch_vendor"
down_revision = "20261105_01_vendor_branch"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    branch_columns = {column["name"] for column in inspector.get_columns("branches")}
    if "vendor_ai_json" not in branch_columns:
        op.add_column("branches", sa.Column("vendor_ai_json", sa.Text(), nullable=True))

    tables = set(sa.inspect(bind).get_table_names())
    if "saas_settings" not in tables:
        op.create_table(
            "saas_settings",
            sa.Column("key", sa.String(length=120), primary_key=True),
            sa.Column("value", sa.Text(), nullable=False, server_default=""),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("updated_by_user_id", sa.Integer(), nullable=True),
            sa.ForeignKeyConstraint(["updated_by_user_id"], ["users.id"], ondelete="SET NULL"),
        )

    if "branch_vendor_licenses" not in tables:
        op.create_table(
            "branch_vendor_licenses",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(), nullable=False),
            sa.Column("branch_id", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="not_contracted"),
            sa.Column("payment_mode", sa.String(length=20), nullable=True),
            sa.Column("amount", sa.Numeric(18, 2), nullable=False, server_default="0.00"),
            sa.Column("currency", sa.String(length=10), nullable=False, server_default="ARS"),
            sa.Column("starts_at", sa.DateTime(), nullable=True),
            sa.Column("ends_at", sa.DateTime(), nullable=True),
            sa.Column("preapproval_id", sa.String(length=120), nullable=True),
            sa.Column("external_reference", sa.String(length=255), nullable=True),
            sa.Column("auto_renew", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("cancel_at_period_end", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("last_payment_id", sa.String(length=120), nullable=True),
            sa.Column("last_payment_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["branch_id"], ["branches.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("branch_id", name="uq_branch_vendor_licenses_branch"),
        )
        op.create_index("ix_branch_vendor_licenses_company_id", "branch_vendor_licenses", ["company_id"])
        op.create_index("ix_branch_vendor_licenses_branch_id", "branch_vendor_licenses", ["branch_id"])
        op.create_index("ix_branch_vendor_licenses_company_status", "branch_vendor_licenses", ["company_id", "status"])
        op.create_index("ix_branch_vendor_licenses_preapproval_id", "branch_vendor_licenses", ["preapproval_id"], unique=True)
        op.create_index("ix_branch_vendor_licenses_external_reference", "branch_vendor_licenses", ["external_reference"], unique=True)
        op.create_index("ix_branch_vendor_licenses_last_payment_id", "branch_vendor_licenses", ["last_payment_id"])


def downgrade():
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "branch_vendor_licenses" in tables:
        indexes = {item.get("name") for item in sa.inspect(bind).get_indexes("branch_vendor_licenses")}
        for name in (
            "ix_branch_vendor_licenses_last_payment_id",
            "ix_branch_vendor_licenses_external_reference",
            "ix_branch_vendor_licenses_preapproval_id",
            "ix_branch_vendor_licenses_company_status",
            "ix_branch_vendor_licenses_branch_id",
            "ix_branch_vendor_licenses_company_id",
        ):
            if name in indexes:
                op.drop_index(name, table_name="branch_vendor_licenses")
        op.drop_table("branch_vendor_licenses")
    if "saas_settings" in tables:
        op.drop_table("saas_settings")
    branch_columns = {column["name"] for column in sa.inspect(bind).get_columns("branches")}
    if "vendor_ai_json" in branch_columns:
        op.drop_column("branches", "vendor_ai_json")
