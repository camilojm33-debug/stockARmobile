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


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "postgresql":
        # Remove legacy global uniqueness.
        op.execute("ALTER TABLE products DROP CONSTRAINT IF EXISTS products_barcode_key")
        op.execute("DROP INDEX IF EXISTS ix_products_barcode")

    # Create tenant-scoped uniqueness.
    op.create_index(
        "ix_products_company_barcode",
        "products",
        ["company_id", "barcode"],
        unique=True,
    )


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    op.drop_index("ix_products_company_barcode", table_name="products")

    # Restore legacy behavior.
    if dialect == "postgresql":
        op.create_unique_constraint("products_barcode_key", "products", ["barcode"])
        op.create_index("ix_products_barcode", "products", ["barcode"], unique=False)
