"""Add list-level quantity discount rules for wholesale price lists."""

from alembic import op
import sqlalchemy as sa


revision = "20261028_01_general_discount_rules"
down_revision = "20261026_02_wholesale_disc_snap"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("price_list_discount_rules"):
        return

    op.create_table(
        "price_list_discount_rules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "company_id",
            sa.Integer(),
            sa.ForeignKey("companies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "price_list_id",
            sa.Integer(),
            sa.ForeignKey("price_lists.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("min_quantity", sa.Numeric(18, 3), nullable=False),
        sa.Column("discount_percent", sa.Numeric(5, 2), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "price_list_id",
            "min_quantity",
            name="uq_price_list_discount_rule_tier",
        ),
    )
    op.create_index(
        "ix_price_list_discount_rules_company_list",
        "price_list_discount_rules",
        ["company_id", "price_list_id", "active"],
    )
    op.create_index(
        "ix_price_list_discount_rules_company_id",
        "price_list_discount_rules",
        ["company_id"],
    )
    op.create_index(
        "ix_price_list_discount_rules_price_list_id",
        "price_list_discount_rules",
        ["price_list_id"],
    )


def downgrade():
    bind = op.get_bind()
    if sa.inspect(bind).has_table("price_list_discount_rules"):
        op.drop_table("price_list_discount_rules")
