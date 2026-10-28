"""Add pickup tracking fields to quote delivery records.

Existing deliveries are labeled legacy because historic pickup completion cannot
be inferred safely. New deliveries default to pending.
"""
from alembic import op
import sqlalchemy as sa

revision = "20261028_02_pickup"
down_revision = "20261028_01_gen_disc_rules"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("quote_deliveries"):
        return

    columns = {item["name"] for item in inspector.get_columns("quote_deliveries")}
    if "pickup_status" not in columns:
        op.add_column("quote_deliveries", sa.Column("pickup_status", sa.String(length=24), nullable=True))
    if "picked_up_at" not in columns:
        op.add_column("quote_deliveries", sa.Column("picked_up_at", sa.DateTime(), nullable=True))
    if "picked_up_by_user_id" not in columns:
        op.add_column("quote_deliveries", sa.Column("picked_up_by_user_id", sa.Integer(), nullable=True))

    # Existing rows predate pickup tracking. They cannot safely be classified as
    # pending or picked up, so retain an explicit legacy state.
    op.execute(sa.text(
        "UPDATE quote_deliveries SET pickup_status = 'legacy' WHERE pickup_status IS NULL"
    ))

    fk_name = "fk_quote_deliveries_picked_up_by_user_id_users"
    fks = {fk.get("name") for fk in sa.inspect(bind).get_foreign_keys("quote_deliveries")}
    if fk_name not in fks:
        op.create_foreign_key(
            fk_name, "quote_deliveries", "users",
            ["picked_up_by_user_id"], ["id"],
        )

    status_column = next(
        (item for item in sa.inspect(bind).get_columns("quote_deliveries") if item["name"] == "pickup_status"),
        None,
    )
    if status_column and status_column.get("nullable", True):
        op.alter_column(
            "quote_deliveries", "pickup_status",
            existing_type=sa.String(length=24),
            nullable=False,
            server_default="pending",
        )
    else:
        op.alter_column(
            "quote_deliveries", "pickup_status",
            existing_type=sa.String(length=24),
            server_default="pending",
        )

    indexes = {item["name"] for item in sa.inspect(bind).get_indexes("quote_deliveries")}
    if "ix_quote_deliveries_pickup_status" not in indexes:
        op.create_index("ix_quote_deliveries_pickup_status", "quote_deliveries", ["pickup_status"])


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("quote_deliveries"):
        return

    fk_name = "fk_quote_deliveries_picked_up_by_user_id_users"
    fks = {fk.get("name") for fk in inspector.get_foreign_keys("quote_deliveries")}
    if fk_name in fks:
        op.drop_constraint(fk_name, "quote_deliveries", type_="foreignkey")

    indexes = {item["name"] for item in sa.inspect(bind).get_indexes("quote_deliveries")}
    if "ix_quote_deliveries_pickup_status" in indexes:
        op.drop_index("ix_quote_deliveries_pickup_status", table_name="quote_deliveries")

    columns = {row["name"] for row in sa.inspect(bind).get_columns("quote_deliveries")}
    for name in ("picked_up_by_user_id", "picked_up_at", "pickup_status"):
        if name in columns:
            op.drop_column("quote_deliveries", name)
