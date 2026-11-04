"""Bind POS payment drafts to their originating branch.

Historical payments intentionally remain NULL: their branch cannot be inferred
safely from the current session after the fact.
"""
from alembic import op
import sqlalchemy as sa

revision = "20261103_01_payment_branch_scope"
down_revision = "20261102_01_multibranch_opening"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("payments"):
        return
    columns = {column["name"] for column in inspector.get_columns("payments")}
    if "branch_id" not in columns:
        op.add_column(
            "payments",
            sa.Column(
                "branch_id",
                sa.Integer(),
                sa.ForeignKey("branches.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("payments")}
    if "ix_payments_branch_id" not in indexes:
        op.create_index("ix_payments_branch_id", "payments", ["branch_id"])


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("payments"):
        return
    indexes = {index["name"] for index in inspector.get_indexes("payments")}
    if "ix_payments_branch_id" in indexes:
        op.drop_index("ix_payments_branch_id", table_name="payments")
    columns = {column["name"] for column in sa.inspect(bind).get_columns("payments")}
    if "branch_id" in columns:
        op.drop_column("payments", "branch_id")
