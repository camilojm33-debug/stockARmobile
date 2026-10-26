"""Add company-scoped wholesale price lists and price-source snapshots."""

from alembic import op
import sqlalchemy as sa

revision = "20261025_01_wholesale_price_lists"
down_revision = "20261024_01_release_readiness"
branch_labels = None
depends_on = None


def _has_table(bind, table_name):
    return sa.inspect(bind).has_table(table_name)


def _columns(bind, table_name):
    if not _has_table(bind, table_name):
        return set()
    return {row["name"] for row in sa.inspect(bind).get_columns(table_name)}


def _indexes(bind, table_name):
    if not _has_table(bind, table_name):
        return set()
    return {row.get("name") for row in sa.inspect(bind).get_indexes(table_name)}


def upgrade():
    bind = op.get_bind()
    if not _has_table(bind, "price_lists"):
        op.create_table(
            "price_lists",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False),
            sa.Column("name", sa.String(length=120), nullable=False),
            sa.Column("code", sa.String(length=80), nullable=False),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("effective_from", sa.Date(), nullable=True),
            sa.Column("effective_until", sa.Date(), nullable=True),
            sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("company_id", "code", name="uq_price_lists_company_code"),
        )
    if "ix_price_lists_company_id" not in _indexes(bind, "price_lists"):
        op.create_index("ix_price_lists_company_id", "price_lists", ["company_id"])
    if "ix_price_lists_company_active" not in _indexes(bind, "price_lists"):
        op.create_index("ix_price_lists_company_active", "price_lists", ["company_id", "active"])

    if not _has_table(bind, "price_list_items"):
        op.create_table(
            "price_list_items",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False),
            sa.Column("price_list_id", sa.Integer(), sa.ForeignKey("price_lists.id", ondelete="CASCADE"), nullable=False),
            sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="CASCADE"), nullable=False),
            sa.Column("min_quantity", sa.Numeric(18, 3), nullable=False, server_default="1.000"),
            sa.Column("unit_price", sa.Numeric(18, 2), nullable=False),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("price_list_id", "product_id", "min_quantity", name="uq_price_list_item_tier"),
        )
    for index_name, columns in (
        ("ix_price_list_items_company_id", ["company_id"]),
        ("ix_price_list_items_price_list_id", ["price_list_id"]),
        ("ix_price_list_items_product_id", ["product_id"]),
        ("ix_price_list_items_company_list_product", ["company_id", "price_list_id", "product_id"]),
    ):
        if index_name not in _indexes(bind, "price_list_items"):
            op.create_index(index_name, "price_list_items", columns)

    # Add fields without overwriting or rebuilding existing customer and document rows.
    if _has_table(bind, "clients"):
        cols = _columns(bind, "clients")
        if "customer_type" not in cols or "price_list_id" not in cols:
            with op.batch_alter_table("clients") as batch:
                if "customer_type" not in cols:
                    batch.add_column(sa.Column("customer_type", sa.String(length=20), nullable=False, server_default="minorista"))
                if "price_list_id" not in cols:
                    batch.add_column(sa.Column("price_list_id", sa.Integer(), nullable=True))
                    batch.create_foreign_key(
                        "fk_clients_price_list_id_price_lists",
                        "price_lists",
                        ["price_list_id"],
                        ["id"],
                        ondelete="SET NULL",
                    )
                    batch.create_index("ix_clients_price_list_id", ["price_list_id"])

    for table_name, fk_name in (
        ("sale_items", "fk_sale_items_price_list_id_price_lists"),
        ("quote_items", "fk_quote_items_price_list_id_price_lists"),
    ):
        if not _has_table(bind, table_name):
            continue
        cols = _columns(bind, table_name)
        if "price_source" not in cols or "price_list_id" not in cols:
            with op.batch_alter_table(table_name) as batch:
                if "price_source" not in cols:
                    batch.add_column(sa.Column("price_source", sa.String(length=20), nullable=False, server_default="minorista"))
                if "price_list_id" not in cols:
                    batch.add_column(sa.Column("price_list_id", sa.Integer(), nullable=True))
                    batch.create_foreign_key(
                        fk_name,
                        "price_lists",
                        ["price_list_id"],
                        ["id"],
                        ondelete="SET NULL",
                    )
                    batch.create_index(f"ix_{table_name}_price_list_id", ["price_list_id"])


def downgrade():
    bind = op.get_bind()
    for table_name, fk_name in (
        ("quote_items", "fk_quote_items_price_list_id_price_lists"),
        ("sale_items", "fk_sale_items_price_list_id_price_lists"),
    ):
        if not _has_table(bind, table_name):
            continue
        cols = _columns(bind, table_name)
        with op.batch_alter_table(table_name) as batch:
            if "price_list_id" in cols:
                try:
                    batch.drop_constraint(fk_name, type_="foreignkey")
                except Exception:
                    pass
                if f"ix_{table_name}_price_list_id" in _indexes(bind, table_name):
                    batch.drop_index(f"ix_{table_name}_price_list_id")
                batch.drop_column("price_list_id")
            if "price_source" in cols:
                batch.drop_column("price_source")

    if _has_table(bind, "clients"):
        cols = _columns(bind, "clients")
        with op.batch_alter_table("clients") as batch:
            if "price_list_id" in cols:
                try:
                    batch.drop_constraint("fk_clients_price_list_id_price_lists", type_="foreignkey")
                except Exception:
                    pass
                if "ix_clients_price_list_id" in _indexes(bind, "clients"):
                    batch.drop_index("ix_clients_price_list_id")
                batch.drop_column("price_list_id")
            if "customer_type" in cols:
                batch.drop_column("customer_type")

    if _has_table(bind, "price_list_items"):
        for index_name in (
            "ix_price_list_items_company_list_product",
            "ix_price_list_items_product_id",
            "ix_price_list_items_price_list_id",
            "ix_price_list_items_company_id",
        ):
            if index_name in _indexes(bind, "price_list_items"):
                op.drop_index(index_name, table_name="price_list_items")
        op.drop_table("price_list_items")
    if _has_table(bind, "price_lists"):
        for index_name in ("ix_price_lists_company_active", "ix_price_lists_company_id"):
            if index_name in _indexes(bind, "price_lists"):
                op.drop_index(index_name, table_name="price_lists")
        op.drop_table("price_lists")
