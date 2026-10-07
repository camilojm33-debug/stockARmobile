"""Persist idempotent public Vendor operations per tenant/conversation."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261007_01_public_vendor_idempotency"
down_revision = "20261006_01_tenant_crm"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    duplicates = bind.execute(sa.text(
        "SELECT company_id, conversation_id, idempotency_key, COUNT(*) "
        "FROM conversation_messages "
        "WHERE idempotency_key IS NOT NULL "
        "GROUP BY company_id, conversation_id, idempotency_key "
        "HAVING COUNT(*) > 1 LIMIT 1"
    )).first()
    if duplicates:
        raise RuntimeError(
            "Duplicate conversation_messages idempotency keys must be inspected before migration: "
            f"company_id={duplicates[0]}, conversation_id={duplicates[1]}"
        )

    op.drop_index("uq_convmsg_company_idempotency", table_name="conversation_messages")
    op.create_index(
        "uq_convmsg_tenant_conversation_idempotency",
        "conversation_messages",
        ["company_id", "conversation_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
        sqlite_where=sa.text("idempotency_key IS NOT NULL"),
    )

    op.create_table(
        "public_vendor_operations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=120), nullable=False),
        sa.Column("operation_type", sa.String(length=24), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("trace_id", sa.String(length=120), nullable=True),
        sa.Column("quote_id", sa.Integer(), nullable=True),
        sa.Column("result_json", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "company_id",
            "conversation_id",
            "idempotency_key",
            name="uq_public_vendor_operation_tenant_conversation_key",
        ),
    )
    op.create_index(
        "ix_public_vendor_operations_status_updated",
        "public_vendor_operations",
        ["status", "updated_at"],
        unique=False,
    )


def downgrade():
    bind = op.get_bind()
    duplicates = bind.execute(sa.text(
        "SELECT company_id, idempotency_key, COUNT(*) "
        "FROM conversation_messages "
        "WHERE idempotency_key IS NOT NULL "
        "GROUP BY company_id, idempotency_key "
        "HAVING COUNT(*) > 1 LIMIT 1"
    )).first()
    if duplicates:
        raise RuntimeError(
            "Cannot restore company-wide idempotency uniqueness without resolving keys shared "
            f"across conversations: company_id={duplicates[0]}"
        )
    op.drop_index("ix_public_vendor_operations_status_updated", table_name="public_vendor_operations")
    op.drop_table("public_vendor_operations")
    op.drop_index("uq_convmsg_tenant_conversation_idempotency", table_name="conversation_messages")
    op.create_index(
        "uq_convmsg_company_idempotency",
        "conversation_messages",
        ["company_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )