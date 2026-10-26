"""Store percentage-based wholesale price tiers."""

from alembic import op
import sqlalchemy as sa


revision = "20261026_01_discount_pct"
down_revision = "20261025_02_wholesale_plan"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("price_list_items"):
        return
    columns = {row["name"] for row in inspector.get_columns("price_list_items")}
    if "discount_percent" not in columns:
        op.add_column(
            "price_list_items",
            sa.Column("discount_percent", sa.Numeric(5, 2), nullable=True),
        )


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("price_list_items"):
        return
    columns = {row["name"] for row in inspector.get_columns("price_list_items")}
    if "discount_percent" in columns:
        op.drop_column("price_list_items", "discount_percent")
