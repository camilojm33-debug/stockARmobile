"""Backfill the opening balances in the multisucursal Kardex.

The original multisucursal migration is already deployed. This additive revision
reconstructs the opening balance as current branch stock minus all recorded
post-migration movements, then records it once in the branch ledger.
"""
from alembic import op
import sqlalchemy as sa

revision = "20261102_01_multibranch_opening"
down_revision = "20261101_01_multibranch"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("branch_stock") or not inspector.has_table("branch_stock_movements"):
        return

    balances = bind.execute(
        sa.text(
            "SELECT bs.company_id,bs.branch_id,bs.product_id,bs.stock "
            "FROM branch_stock bs "
            "JOIN branches b ON b.id=bs.branch_id AND b.company_id=bs.company_id "
            "ORDER BY bs.company_id,bs.branch_id,bs.product_id"
        )
    ).mappings().all()

    for balance in balances:
        existing_opening = bind.execute(
            sa.text(
                "SELECT id FROM branch_stock_movements "
                "WHERE company_id=:company_id AND branch_id=:branch_id "
                "AND product_id=:product_id AND movement_type='opening' LIMIT 1"
            ),
            {
                "company_id": balance["company_id"],
                "branch_id": balance["branch_id"],
                "product_id": balance["product_id"],
            },
        ).first()
        if existing_opening:
            continue

        recorded_delta = bind.execute(
            sa.text(
                "SELECT COALESCE(SUM(quantity_delta),0) "
                "FROM branch_stock_movements "
                "WHERE company_id=:company_id AND branch_id=:branch_id AND product_id=:product_id"
            ),
            {
                "company_id": balance["company_id"],
                "branch_id": balance["branch_id"],
                "product_id": balance["product_id"],
            },
        ).scalar_one()
        opening_quantity = balance["stock"] - recorded_delta
        if opening_quantity < 0:
            raise RuntimeError(
                "Cannot reconstruct a non-negative opening stock for "
                f"company={balance['company_id']} branch={balance['branch_id']} "
                f"product={balance['product_id']}; current stock is lower than its recorded movements."
            )

        bind.execute(
            sa.text(
                "INSERT INTO branch_stock_movements("
                "company_id,branch_id,product_id,movement_type,quantity_delta,stock_after,"
                "source_type,source_id,user_id,note,created_at"
                ") VALUES(:company_id,:branch_id,:product_id,'opening',:quantity,:stock_after,"
                "'multibranch_opening_backfill',NULL,NULL,:note,CURRENT_TIMESTAMP)"
            ),
            {
                "company_id": balance["company_id"],
                "branch_id": balance["branch_id"],
                "product_id": balance["product_id"],
                "quantity": opening_quantity,
                "stock_after": opening_quantity,
                "note": "Saldo de apertura reconstruido al completar el Kardex multisucursal",
            },
        )

    # The persisted branch ledger is authoritative for the consolidated value.
    bind.execute(
        sa.text(
            "UPDATE products SET stock=("
            "SELECT COALESCE(SUM(bs.stock),0) FROM branch_stock bs "
            "WHERE bs.company_id=products.company_id AND bs.product_id=products.id"
            ") WHERE EXISTS ("
            "SELECT 1 FROM branch_stock bs "
            "WHERE bs.company_id=products.company_id AND bs.product_id=products.id"
            ")"
        )
    )


def downgrade():
    # Preserve opening balances and the audit trail after rollout.
    pass
