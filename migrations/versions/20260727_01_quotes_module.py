"""add quotes module tables

Revision ID: 20260727_01_quotes_module
Revises: 20260716_04_sales_comprobante_fields
Create Date: 2026-07-27 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260727_01_quotes_module"
down_revision = "20260716_04_sales_comprobante_fields"
branch_labels = None
depends_on = None


def _has_table(bind, table_name):
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def _has_column(bind, table_name, column_name):
    inspector = sa.inspect(bind)
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def upgrade():
    bind = op.get_bind()

    if not _has_table(bind, "quotes"):
        op.create_table(
            "quotes",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("number", sa.String(length=40), nullable=True),
            sa.Column("date", sa.DateTime(), nullable=True),
            sa.Column("expires_at", sa.DateTime(), nullable=True),
            sa.Column("subtotal", sa.Numeric(18, 2), nullable=True),
            sa.Column("discount", sa.Numeric(18, 2), nullable=True),
            sa.Column("surcharge", sa.Numeric(18, 2), nullable=True),
            sa.Column("tax", sa.Numeric(18, 2), nullable=True),
            sa.Column("total_amount", sa.Numeric(18, 2), nullable=True),
            sa.Column("observations", sa.Text(), nullable=True),
            sa.Column("commercial_conditions", sa.Text(), nullable=True),
            sa.Column("status", sa.String(length=30), nullable=True),
            sa.Column("currency", sa.String(length=10), nullable=True),
            sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id"), nullable=True),
            sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("seller_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=True),
            sa.Column("branch_id", sa.Integer(), nullable=True),
            sa.Column("converted_sale_id", sa.Integer(), sa.ForeignKey("sales.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_quotes_company_status_date", "quotes", ["company_id", "status", "date"], unique=False)
        op.create_index("ix_quotes_company_number", "quotes", ["company_id", "number"], unique=True)
        op.create_index("ix_quotes_company_id", "quotes", ["company_id"], unique=False)
        op.create_index("ix_quotes_client_id", "quotes", ["client_id"], unique=False)
        op.create_index("ix_quotes_seller_id", "quotes", ["seller_id"], unique=False)
        op.create_index("ix_quotes_converted_sale_id", "quotes", ["converted_sale_id"], unique=False)
    else:
        if not _has_column(bind, "quotes", "commercial_conditions"):
            op.add_column("quotes", sa.Column("commercial_conditions", sa.Text(), nullable=True))
        if not _has_column(bind, "quotes", "currency"):
            op.add_column("quotes", sa.Column("currency", sa.String(length=10), nullable=True))
        if not _has_column(bind, "quotes", "created_by_user_id"):
            op.add_column("quotes", sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True))
        if not _has_column(bind, "quotes", "branch_id"):
            op.add_column("quotes", sa.Column("branch_id", sa.Integer(), nullable=True))
        index_names = {index["name"] for index in sa.inspect(bind).get_indexes("quotes")}
        if "ix_quotes_number" in index_names:
            op.drop_index("ix_quotes_number", table_name="quotes")
        if "ix_quotes_company_number" not in index_names:
            op.create_index("ix_quotes_company_number", "quotes", ["company_id", "number"], unique=True)

    if not _has_table(bind, "quote_items"):
        op.create_table(
            "quote_items",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("quote_id", sa.Integer(), sa.ForeignKey("quotes.id"), nullable=False),
            sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=True),
            sa.Column("description", sa.String(length=255), nullable=False),
            sa.Column("quantity", sa.Float(), nullable=False),
            sa.Column("unit_price", sa.Numeric(18, 2), nullable=False),
            sa.Column("discount", sa.Numeric(18, 2), nullable=True),
            sa.Column("subtotal", sa.Numeric(18, 2), nullable=True),
            sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        )
        op.create_index("ix_quote_items_quote_id", "quote_items", ["quote_id"], unique=False)
        op.create_index("ix_quote_items_product_id", "quote_items", ["product_id"], unique=False)
    else:
        if not _has_column(bind, "quote_items", "sort_order"):
            op.add_column("quote_items", sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"))


def downgrade():
    bind = op.get_bind()

    if _has_table(bind, "quote_items"):
        op.drop_index("ix_quote_items_product_id", table_name="quote_items")
        op.drop_index("ix_quote_items_quote_id", table_name="quote_items")
        op.drop_table("quote_items")

    if _has_table(bind, "quotes"):
        op.drop_index("ix_quotes_converted_sale_id", table_name="quotes")
        op.drop_index("ix_quotes_seller_id", table_name="quotes")
        op.drop_index("ix_quotes_client_id", table_name="quotes")
        op.drop_index("ix_quotes_company_id", table_name="quotes")
        op.drop_index("ix_quotes_number", table_name="quotes")
        op.drop_index("ix_quotes_company_status_date", table_name="quotes")
        op.drop_table("quotes")
