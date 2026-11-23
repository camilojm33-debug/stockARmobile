"""Track each prospect row processed by a SuperAdmin CRM import.

Revision ID: 20261123_02_saas_lead_import_records
Revises: 20261123_01_quote_item_pickup
Create Date: 2026-11-23
"""
from alembic import op
import sqlalchemy as sa


revision = "20261123_02_saas_lead_import_records"
down_revision = "20261123_01_quote_item_pickup"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if sa.inspect(bind).has_table("saas_lead_import_records"):
        return
    op.create_table(
        "saas_lead_import_records",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "import_id",
            sa.Integer(),
            sa.ForeignKey("saas_lead_imports.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "lead_id",
            sa.Integer(),
            sa.ForeignKey("saas_leads.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("row_number", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(length=20), nullable=False),
        sa.Column("company_name", sa.String(length=160)),
        sa.Column("contact_name", sa.String(length=160)),
        sa.Column("email", sa.String(length=160)),
        sa.Column("phone", sa.String(length=40)),
        sa.Column("whatsapp", sa.String(length=40)),
        sa.Column("industry", sa.String(length=100)),
        sa.Column("province", sa.String(length=100)),
        sa.Column("locality", sa.String(length=120)),
        sa.Column("details", sa.String(length=500)),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("import_id", "row_number", name="uq_saas_lead_import_row"),
    )
    op.create_index("ix_saas_lead_import_records_import_id", "saas_lead_import_records", ["import_id"])
    op.create_index("ix_saas_lead_import_records_lead_id", "saas_lead_import_records", ["lead_id"])
    op.create_index("ix_saas_lead_import_records_outcome", "saas_lead_import_records", ["outcome"])
    op.create_index("ix_saas_lead_import_records_created_at", "saas_lead_import_records", ["created_at"])


def downgrade():
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("saas_lead_import_records"):
        return
    op.drop_table("saas_lead_import_records")
