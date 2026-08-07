"""harden purchases company scope

Revision ID: 20260806_01_harden_purchases_company_scope
Revises: 20260729_02_business_billing_documents
Create Date: 2026-08-06 12:10:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260806_01_harden_purchases_company_scope"
down_revision = "20260729_02_business_billing_documents"
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


def _nullable_company_count(bind, table_name):
    return int(
        bind.execute(sa.text(f"SELECT COUNT(*) FROM {table_name} WHERE company_id IS NULL")).scalar() or 0
    )


def upgrade():
    bind = op.get_bind()

    if not _table_exists(bind, "suppliers") or not _table_exists(bind, "purchase_orders"):
        return

    # Backfill purchase_orders.company_id from supplier ownership when available.
    bind.execute(
        sa.text(
            """
            UPDATE purchase_orders
            SET company_id = (
                SELECT suppliers.company_id
                FROM suppliers
                WHERE suppliers.id = purchase_orders.supplier_id
            )
            WHERE purchase_orders.company_id IS NULL
              AND purchase_orders.supplier_id IS NOT NULL
            """
        )
    )

    # Secondary backfill from purchase_items -> products when supplier is missing.
    if _table_exists(bind, "purchase_items") and _table_exists(bind, "products"):
        bind.execute(
            sa.text(
                """
                UPDATE purchase_orders
                SET company_id = (
                    SELECT products.company_id
                    FROM purchase_items
                    JOIN products ON products.id = purchase_items.product_id
                    WHERE purchase_items.purchase_order_id = purchase_orders.id
                      AND products.company_id IS NOT NULL
                    LIMIT 1
                )
                WHERE purchase_orders.company_id IS NULL
                """
            )
        )

    # Backfill suppliers.company_id from related purchase orders if possible.
    bind.execute(
        sa.text(
            """
            UPDATE suppliers
            SET company_id = (
                SELECT purchase_orders.company_id
                FROM purchase_orders
                WHERE purchase_orders.supplier_id = suppliers.id
                  AND purchase_orders.company_id IS NOT NULL
                LIMIT 1
            )
            WHERE suppliers.company_id IS NULL
            """
        )
    )

    missing_suppliers = _nullable_company_count(bind, "suppliers")
    missing_purchase_orders = _nullable_company_count(bind, "purchase_orders")
    if missing_suppliers or missing_purchase_orders:
        raise RuntimeError(
            "No se pudo endurecer purchases multi-tenant: existen rows sin company_id "
            f"(suppliers={missing_suppliers}, purchase_orders={missing_purchase_orders}). "
            "Completa la asignación manual de empresa antes de aplicar esta migración."
        )

    with op.batch_alter_table("suppliers") as batch_op:
        batch_op.alter_column("company_id", existing_type=sa.Integer(), nullable=False)
    with op.batch_alter_table("purchase_orders") as batch_op:
        batch_op.alter_column("company_id", existing_type=sa.Integer(), nullable=False)

    if not _index_exists(bind, "suppliers", "ix_suppliers_company_id"):
        op.create_index("ix_suppliers_company_id", "suppliers", ["company_id"], unique=False)
    if not _index_exists(bind, "purchase_orders", "ix_purchase_orders_company_id"):
        op.create_index("ix_purchase_orders_company_id", "purchase_orders", ["company_id"], unique=False)


def downgrade():
    bind = op.get_bind()

    if _table_exists(bind, "purchase_orders") and _index_exists(bind, "purchase_orders", "ix_purchase_orders_company_id"):
        op.drop_index("ix_purchase_orders_company_id", table_name="purchase_orders")
    if _table_exists(bind, "suppliers") and _index_exists(bind, "suppliers", "ix_suppliers_company_id"):
        op.drop_index("ix_suppliers_company_id", table_name="suppliers")

    if _table_exists(bind, "purchase_orders"):
        with op.batch_alter_table("purchase_orders") as batch_op:
            batch_op.alter_column("company_id", existing_type=sa.Integer(), nullable=True)
    if _table_exists(bind, "suppliers"):
        with op.batch_alter_table("suppliers") as batch_op:
            batch_op.alter_column("company_id", existing_type=sa.Integer(), nullable=True)
