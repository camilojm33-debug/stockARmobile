"""Enforce one Mercado Pago seller account per StockArMobile company linkage.

Revision ID: 20260922_04_mercadopago_company_isolation
Revises: 20260922_03_promotions
"""

from alembic import op
import sqlalchemy as sa


revision = "20260922_04_mercadopago_company_isolation"
down_revision = "20260922_03_promotions"
branch_labels = None
depends_on = None


def _has_table(bind, table_name):
    return table_name in sa.inspect(bind).get_table_names()


def _has_index(bind, table_name, index_name):
    inspector = sa.inspect(bind)
    if table_name not in inspector.get_table_names():
        return False
    return index_name in {index["name"] for index in inspector.get_indexes(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_table(bind, "mercadopago_connections"):
        return

    # NULL is intentionally allowed so a disconnected company can later link the
    # same seller again. The application clears mp_user_id on disconnect.
    if not _has_index(bind, "mercadopago_connections", "uq_mercadopago_connections_mp_user_id"):
        op.create_index(
            "uq_mercadopago_connections_mp_user_id",
            "mercadopago_connections",
            ["mp_user_id"],
            unique=True,
            postgresql_where=sa.text("mp_user_id IS NOT NULL"),
            sqlite_where=sa.text("mp_user_id IS NOT NULL"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _has_table(bind, "mercadopago_connections") and _has_index(bind, "mercadopago_connections", "uq_mercadopago_connections_mp_user_id"):
        op.drop_index("uq_mercadopago_connections_mp_user_id", table_name="mercadopago_connections")
