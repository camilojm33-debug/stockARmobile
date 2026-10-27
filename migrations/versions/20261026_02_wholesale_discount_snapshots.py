"""Persist wholesale discount display snapshots for sale and quote lines."""

from alembic import op
import sqlalchemy as sa


revision = "20261026_02_wholesale_disc_snap"
down_revision = "20261026_01_discount_pct"
branch_labels = None
depends_on = None


def _add_column_if_missing(table_name, column):
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(table_name):
        return
    columns = {row["name"] for row in inspector.get_columns(table_name)}
    if column.name not in columns:
        op.add_column(table_name, column)


def upgrade():
    _add_column_if_missing(
        "sale_items",
        sa.Column("wholesale_discount_percent", sa.Numeric(5, 2), nullable=True),
    )
    _add_column_if_missing(
        "sale_items",
        sa.Column("wholesale_reference_price", sa.Numeric(18, 2), nullable=True),
    )
    _add_column_if_missing(
        "quote_items",
        sa.Column("wholesale_discount_percent", sa.Numeric(5, 2), nullable=True),
    )
    _add_column_if_missing(
        "quote_items",
        sa.Column("wholesale_reference_price", sa.Numeric(18, 2), nullable=True),
    )


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for table_name, column_name in (
        ("quote_items", "wholesale_reference_price"),
        ("quote_items", "wholesale_discount_percent"),
        ("sale_items", "wholesale_reference_price"),
        ("sale_items", "wholesale_discount_percent"),
    ):
        if not inspector.has_table(table_name):
            continue
        columns = {row["name"] for row in sa.inspect(bind).get_columns(table_name)}
        if column_name in columns:
            op.drop_column(table_name, column_name)
