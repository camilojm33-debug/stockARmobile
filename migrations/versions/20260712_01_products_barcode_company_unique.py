"""Make product barcode unique per company.

Revision ID: 20260712_01
Revises: 
Create Date: 2026-07-12
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260712_01"
down_revision = None
branch_labels = None
depends_on = None


def _has_table(bind, table_name):
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def _has_index(bind, table_name, index_name):
    inspector = sa.inspect(bind)
    if table_name not in inspector.get_table_names():
        return False
    return index_name in {index["name"] for index in inspector.get_indexes(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    # Some legacy environments started without a full Alembic baseline.
    # Skip unsafe operations when foundational tables are missing.
    if not _has_table(bind, "products"):
        return

    if dialect == "postgresql":
        # Remove legacy global uniqueness.
        op.execute("ALTER TABLE products DROP CONSTRAINT IF EXISTS products_barcode_key")
        op.execute("DROP INDEX IF EXISTS ix_products_barcode")

    # Create tenant-scoped uniqueness.
    if not _has_index(bind, "products", "ix_products_company_barcode"):
        op.create_index(
            "ix_products_company_barcode",
            "products",
            ["company_id", "barcode"],
            unique=True,
        )


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if _has_table(bind, "products") and _has_index(bind, "products", "ix_products_company_barcode"):
        op.drop_index("ix_products_company_barcode", table_name="products")

    # Restore legacy behavior.
    if dialect == "postgresql":
        op.create_unique_constraint("products_barcode_key", "products", ["barcode"])
        op.create_index("ix_products_barcode", "products", ["barcode"], unique=False)
