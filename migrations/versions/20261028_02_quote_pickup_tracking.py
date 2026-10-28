"""Add pickup tracking fields to existing quote delivery records."""
from alembic import op
import sqlalchemy as sa

revision = "20261028_02_quote_pickup_tracking"
down_revision = "20261028_01_gen_disc_rules"
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
    _add_column_if_missing("quote_deliveries", sa.Column("pickup_status", sa.String(length=24), nullable=True))
    _add_column_if_missing("quote_deliveries", sa.Column("picked_up_at", sa.DateTime(), nullable=True))
    _add_column_if_missing("quote_deliveries", sa.Column("picked_up_by_user_id", sa.Integer(), nullable=True))
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("quote_deliveries"):
        indexes = {item["name"] for item in inspector.get_indexes("quote_deliveries")}
        if "ix_quote_deliveries_pickup_status" not in indexes:
            op.create_index("ix_quote_deliveries_pickup_status", "quote_deliveries", ["pickup_status"])


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("quote_deliveries"):
        return
    indexes = {item["name"] for item in inspector.get_indexes("quote_deliveries")}
    if "ix_quote_deliveries_pickup_status" in indexes:
        op.drop_index("ix_quote_deliveries_pickup_status", table_name="quote_deliveries")
    columns = {row["name"] for row in sa.inspect(bind).get_columns("quote_deliveries")}
    for name in ("picked_up_by_user_id", "picked_up_at", "pickup_status"):
        if name in columns:
            op.drop_column("quote_deliveries", name)
