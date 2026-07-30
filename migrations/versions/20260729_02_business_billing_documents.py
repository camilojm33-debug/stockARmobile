"""business billing documents

Revision ID: 20260729_02_business_billing_documents
Revises: 20260729_01_subscription_commands_hardening
Create Date: 2026-07-29 23:40:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260729_02_business_billing_documents"
down_revision = "20260729_01_subscription_commands_hardening"
branch_labels = None
depends_on = None


def _table_exists(bind, table_name):
    inspector = sa.inspect(bind)
    try:
        return table_name in inspector.get_table_names()
    except Exception:
        return False


def _index_exists(bind, table_name, index_name):
    inspector = sa.inspect(bind)
    try:
        indexes = inspector.get_indexes(table_name)
    except Exception:
        return False
    return any((item.get("name") or "") == index_name for item in indexes)


def upgrade():
    bind = op.get_bind()

    if not _table_exists(bind, "business_document_sequences"):
        op.create_table(
            "business_document_sequences",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False),
            sa.Column("doc_type", sa.String(length=40), nullable=False),
            sa.Column("pos_number", sa.String(length=5), nullable=False),
            sa.Column("current_number", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
        )

    if not _index_exists(bind, "business_document_sequences", "ix_bd_sequences_company_doctype_pos"):
        op.create_index(
            "ix_bd_sequences_company_doctype_pos",
            "business_document_sequences",
            ["company_id", "doc_type", "pos_number"],
            unique=True,
        )

    if not _table_exists(bind, "business_documents"):
        op.create_table(
            "business_documents",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False),
            sa.Column("source_type", sa.String(length=20), nullable=False, server_default="sale"),
            sa.Column("source_id", sa.Integer(), nullable=False),
            sa.Column("doc_type", sa.String(length=40), nullable=False),
            sa.Column("pos_number", sa.String(length=5), nullable=False),
            sa.Column("seq_number", sa.Integer(), nullable=False),
            sa.Column("document_number", sa.String(length=20), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="emitido"),
            sa.Column("client_name", sa.String(length=200), nullable=True),
            sa.Column("client_tax_id", sa.String(length=50), nullable=True),
            sa.Column("total_amount", sa.Numeric(18, 2), nullable=True, server_default="0"),
            sa.Column("currency", sa.String(length=10), nullable=True, server_default="ARS"),
            sa.Column("branch_label", sa.String(length=120), nullable=True),
            sa.Column("emitted_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("metadata_json", sa.Text(), nullable=True),
            sa.Column("issued_at", sa.DateTime(), nullable=True),
            sa.Column("annulled_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
        )

    if not _index_exists(bind, "business_documents", "ix_bd_company_date"):
        op.create_index("ix_bd_company_date", "business_documents", ["company_id", "issued_at"], unique=False)
    if not _index_exists(bind, "business_documents", "ix_bd_company_status"):
        op.create_index("ix_bd_company_status", "business_documents", ["company_id", "status"], unique=False)
    if not _index_exists(bind, "business_documents", "ix_bd_company_source"):
        op.create_index("ix_bd_company_source", "business_documents", ["company_id", "source_type", "source_id"], unique=False)
    if not _index_exists(bind, "business_documents", "ix_bd_company_number"):
        op.create_index("ix_bd_company_number", "business_documents", ["company_id", "document_number"], unique=True)
    if not _index_exists(bind, "business_documents", "ix_bd_company_doctype_pos_seq"):
        op.create_index(
            "ix_bd_company_doctype_pos_seq",
            "business_documents",
            ["company_id", "doc_type", "pos_number", "seq_number"],
            unique=True,
        )


def downgrade():
    bind = op.get_bind()

    if _index_exists(bind, "business_documents", "ix_bd_company_doctype_pos_seq"):
        op.drop_index("ix_bd_company_doctype_pos_seq", table_name="business_documents")
    if _index_exists(bind, "business_documents", "ix_bd_company_number"):
        op.drop_index("ix_bd_company_number", table_name="business_documents")
    if _index_exists(bind, "business_documents", "ix_bd_company_source"):
        op.drop_index("ix_bd_company_source", table_name="business_documents")
    if _index_exists(bind, "business_documents", "ix_bd_company_status"):
        op.drop_index("ix_bd_company_status", table_name="business_documents")
    if _index_exists(bind, "business_documents", "ix_bd_company_date"):
        op.drop_index("ix_bd_company_date", table_name="business_documents")
    if _table_exists(bind, "business_documents"):
        op.drop_table("business_documents")

    if _index_exists(bind, "business_document_sequences", "ix_bd_sequences_company_doctype_pos"):
        op.drop_index("ix_bd_sequences_company_doctype_pos", table_name="business_document_sequences")
    if _table_exists(bind, "business_document_sequences"):
        op.drop_table("business_document_sequences")
