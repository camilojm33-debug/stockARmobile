"""Harden monetary columns using NUMERIC and add company QR payment fields.

Revision ID: 20260712_02
Revises: 20260712_01
Create Date: 2026-07-12
"""

from alembic import op
import sqlalchemy as sa


revision = "20260712_02"
down_revision = "20260712_01"
branch_labels = None
depends_on = None


def _has_column(bind, table_name, column_name):
    inspector = sa.inspect(bind)
    if table_name not in inspector.get_table_names():
        return False
    return column_name in {column["name"] for column in inspector.get_columns(table_name)}


def _add_company_qr_columns(bind):
    if not _has_column(bind, "companies", "payment_alias"):
        op.add_column("companies", sa.Column("payment_alias", sa.String(length=120), nullable=True))
    if not _has_column(bind, "companies", "payment_cbu"):
        op.add_column("companies", sa.Column("payment_cbu", sa.String(length=40), nullable=True))
    if not _has_column(bind, "companies", "payment_cvu"):
        op.add_column("companies", sa.Column("payment_cvu", sa.String(length=40), nullable=True))
    if not _has_column(bind, "companies", "payment_qr_text"):
        op.add_column("companies", sa.Column("payment_qr_text", sa.String(length=255), nullable=True))
    if not _has_column(bind, "companies", "payment_qr_url"):
        op.add_column("companies", sa.Column("payment_qr_url", sa.String(length=255), nullable=True))


def _upgrade_numeric_postgresql(bind):
    if bind.dialect.name != "postgresql":
        return

    statements = [
        "ALTER TABLE products ALTER COLUMN cost_price TYPE NUMERIC(18,2) USING ROUND(COALESCE(cost_price,0)::numeric,2)",
        "ALTER TABLE products ALTER COLUMN price TYPE NUMERIC(18,2) USING ROUND(COALESCE(price,0)::numeric,2)",
        "ALTER TABLE products ALTER COLUMN margin TYPE NUMERIC(18,2) USING ROUND(COALESCE(margin,0)::numeric,2)",
        "ALTER TABLE products ALTER COLUMN profit_percent TYPE NUMERIC(10,4) USING ROUND(COALESCE(profit_percent,0)::numeric,4)",
        "ALTER TABLE products ALTER COLUMN tax TYPE NUMERIC(10,4) USING ROUND(COALESCE(tax,0)::numeric,4)",
        "ALTER TABLE products ALTER COLUMN discount TYPE NUMERIC(18,2) USING ROUND(COALESCE(discount,0)::numeric,2)",
        "ALTER TABLE clients ALTER COLUMN balance TYPE NUMERIC(18,2) USING ROUND(COALESCE(balance,0)::numeric,2)",
        "ALTER TABLE clients ALTER COLUMN credit_limit TYPE NUMERIC(18,2) USING ROUND(COALESCE(credit_limit,0)::numeric,2)",
        "ALTER TABLE sales ALTER COLUMN subtotal TYPE NUMERIC(18,2) USING ROUND(COALESCE(subtotal,0)::numeric,2)",
        "ALTER TABLE sales ALTER COLUMN discount TYPE NUMERIC(18,2) USING ROUND(COALESCE(discount,0)::numeric,2)",
        "ALTER TABLE sales ALTER COLUMN tax TYPE NUMERIC(18,2) USING ROUND(COALESCE(tax,0)::numeric,2)",
        "ALTER TABLE sales ALTER COLUMN total_amount TYPE NUMERIC(18,2) USING ROUND(COALESCE(total_amount,0)::numeric,2)",
        "ALTER TABLE sales ALTER COLUMN paid_amount TYPE NUMERIC(18,2) USING ROUND(COALESCE(paid_amount,0)::numeric,2)",
        "ALTER TABLE sales ALTER COLUMN secondary_paid_amount TYPE NUMERIC(18,2) USING ROUND(COALESCE(secondary_paid_amount,0)::numeric,2)",
        "ALTER TABLE sales ALTER COLUMN surcharge TYPE NUMERIC(18,2) USING ROUND(COALESCE(surcharge,0)::numeric,2)",
        "ALTER TABLE sale_items ALTER COLUMN price TYPE NUMERIC(18,2) USING ROUND(COALESCE(price,0)::numeric,2)",
        "ALTER TABLE sale_items ALTER COLUMN cost_price TYPE NUMERIC(18,2) USING ROUND(COALESCE(cost_price,0)::numeric,2)",
        "ALTER TABLE sale_items ALTER COLUMN discount TYPE NUMERIC(18,2) USING ROUND(COALESCE(discount,0)::numeric,2)",
        "ALTER TABLE plans ALTER COLUMN price TYPE NUMERIC(18,2) USING ROUND(COALESCE(price,0)::numeric,2)",
        "ALTER TABLE invoices ALTER COLUMN amount TYPE NUMERIC(18,2) USING ROUND(COALESCE(amount,0)::numeric,2)",
        "ALTER TABLE invoices ALTER COLUMN vat_amount TYPE NUMERIC(18,2) USING ROUND(COALESCE(vat_amount,0)::numeric,2)",
        "ALTER TABLE payments ALTER COLUMN amount TYPE NUMERIC(18,2) USING ROUND(COALESCE(amount,0)::numeric,2)",
        "ALTER TABLE purchase_orders ALTER COLUMN subtotal TYPE NUMERIC(18,2) USING ROUND(COALESCE(subtotal,0)::numeric,2)",
        "ALTER TABLE purchase_orders ALTER COLUMN total_amount TYPE NUMERIC(18,2) USING ROUND(COALESCE(total_amount,0)::numeric,2)",
        "ALTER TABLE purchase_items ALTER COLUMN unit_cost TYPE NUMERIC(18,2) USING ROUND(COALESCE(unit_cost,0)::numeric,2)",
        "ALTER TABLE cash_sessions ALTER COLUMN opening_amount TYPE NUMERIC(18,2) USING ROUND(COALESCE(opening_amount,0)::numeric,2)",
        "ALTER TABLE cash_sessions ALTER COLUMN closing_amount TYPE NUMERIC(18,2) USING ROUND(COALESCE(closing_amount,0)::numeric,2)",
        "ALTER TABLE cash_sessions ALTER COLUMN expected_amount TYPE NUMERIC(18,2) USING ROUND(COALESCE(expected_amount,0)::numeric,2)",
        "ALTER TABLE cash_movements ALTER COLUMN amount TYPE NUMERIC(18,2) USING ROUND(COALESCE(amount,0)::numeric,2)",
        "ALTER TABLE expenses ALTER COLUMN amount TYPE NUMERIC(18,2) USING ROUND(COALESCE(amount,0)::numeric,2)",
        "ALTER TABLE product_price_history ALTER COLUMN old_price TYPE NUMERIC(18,2) USING ROUND(COALESCE(old_price,0)::numeric,2)",
        "ALTER TABLE product_price_history ALTER COLUMN new_price TYPE NUMERIC(18,2) USING ROUND(COALESCE(new_price,0)::numeric,2)",
        "ALTER TABLE product_price_history ALTER COLUMN old_cost TYPE NUMERIC(18,2) USING ROUND(COALESCE(old_cost,0)::numeric,2)",
        "ALTER TABLE product_price_history ALTER COLUMN new_cost TYPE NUMERIC(18,2) USING ROUND(COALESCE(new_cost,0)::numeric,2)",
    ]
    for statement in statements:
        op.execute(statement)


def _downgrade_numeric_postgresql(bind):
    if bind.dialect.name != "postgresql":
        return
    statements = [
        "ALTER TABLE products ALTER COLUMN cost_price TYPE DOUBLE PRECISION USING cost_price::double precision",
        "ALTER TABLE products ALTER COLUMN price TYPE DOUBLE PRECISION USING price::double precision",
        "ALTER TABLE products ALTER COLUMN margin TYPE DOUBLE PRECISION USING margin::double precision",
        "ALTER TABLE products ALTER COLUMN profit_percent TYPE DOUBLE PRECISION USING profit_percent::double precision",
        "ALTER TABLE products ALTER COLUMN tax TYPE DOUBLE PRECISION USING tax::double precision",
        "ALTER TABLE products ALTER COLUMN discount TYPE DOUBLE PRECISION USING discount::double precision",
        "ALTER TABLE clients ALTER COLUMN balance TYPE DOUBLE PRECISION USING balance::double precision",
        "ALTER TABLE clients ALTER COLUMN credit_limit TYPE DOUBLE PRECISION USING credit_limit::double precision",
        "ALTER TABLE sales ALTER COLUMN subtotal TYPE DOUBLE PRECISION USING subtotal::double precision",
        "ALTER TABLE sales ALTER COLUMN discount TYPE DOUBLE PRECISION USING discount::double precision",
        "ALTER TABLE sales ALTER COLUMN tax TYPE DOUBLE PRECISION USING tax::double precision",
        "ALTER TABLE sales ALTER COLUMN total_amount TYPE DOUBLE PRECISION USING total_amount::double precision",
        "ALTER TABLE sales ALTER COLUMN paid_amount TYPE DOUBLE PRECISION USING paid_amount::double precision",
        "ALTER TABLE sales ALTER COLUMN secondary_paid_amount TYPE DOUBLE PRECISION USING secondary_paid_amount::double precision",
        "ALTER TABLE sales ALTER COLUMN surcharge TYPE DOUBLE PRECISION USING surcharge::double precision",
        "ALTER TABLE sale_items ALTER COLUMN price TYPE DOUBLE PRECISION USING price::double precision",
        "ALTER TABLE sale_items ALTER COLUMN cost_price TYPE DOUBLE PRECISION USING cost_price::double precision",
        "ALTER TABLE sale_items ALTER COLUMN discount TYPE DOUBLE PRECISION USING discount::double precision",
        "ALTER TABLE plans ALTER COLUMN price TYPE DOUBLE PRECISION USING price::double precision",
        "ALTER TABLE invoices ALTER COLUMN amount TYPE DOUBLE PRECISION USING amount::double precision",
        "ALTER TABLE invoices ALTER COLUMN vat_amount TYPE DOUBLE PRECISION USING vat_amount::double precision",
        "ALTER TABLE payments ALTER COLUMN amount TYPE DOUBLE PRECISION USING amount::double precision",
        "ALTER TABLE purchase_orders ALTER COLUMN subtotal TYPE DOUBLE PRECISION USING subtotal::double precision",
        "ALTER TABLE purchase_orders ALTER COLUMN total_amount TYPE DOUBLE PRECISION USING total_amount::double precision",
        "ALTER TABLE purchase_items ALTER COLUMN unit_cost TYPE DOUBLE PRECISION USING unit_cost::double precision",
        "ALTER TABLE cash_sessions ALTER COLUMN opening_amount TYPE DOUBLE PRECISION USING opening_amount::double precision",
        "ALTER TABLE cash_sessions ALTER COLUMN closing_amount TYPE DOUBLE PRECISION USING closing_amount::double precision",
        "ALTER TABLE cash_sessions ALTER COLUMN expected_amount TYPE DOUBLE PRECISION USING expected_amount::double precision",
        "ALTER TABLE cash_movements ALTER COLUMN amount TYPE DOUBLE PRECISION USING amount::double precision",
        "ALTER TABLE expenses ALTER COLUMN amount TYPE DOUBLE PRECISION USING amount::double precision",
        "ALTER TABLE product_price_history ALTER COLUMN old_price TYPE DOUBLE PRECISION USING old_price::double precision",
        "ALTER TABLE product_price_history ALTER COLUMN new_price TYPE DOUBLE PRECISION USING new_price::double precision",
        "ALTER TABLE product_price_history ALTER COLUMN old_cost TYPE DOUBLE PRECISION USING old_cost::double precision",
        "ALTER TABLE product_price_history ALTER COLUMN new_cost TYPE DOUBLE PRECISION USING new_cost::double precision",
    ]
    for statement in statements:
        op.execute(statement)


def upgrade() -> None:
    bind = op.get_bind()
    _add_company_qr_columns(bind)
    _upgrade_numeric_postgresql(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _downgrade_numeric_postgresql(bind)

    if _has_column(bind, "companies", "payment_qr_url"):
        op.drop_column("companies", "payment_qr_url")
    if _has_column(bind, "companies", "payment_qr_text"):
        op.drop_column("companies", "payment_qr_text")
    if _has_column(bind, "companies", "payment_cvu"):
        op.drop_column("companies", "payment_cvu")
    if _has_column(bind, "companies", "payment_cbu"):
        op.drop_column("companies", "payment_cbu")
    if _has_column(bind, "companies", "payment_alias"):
        op.drop_column("companies", "payment_alias")
