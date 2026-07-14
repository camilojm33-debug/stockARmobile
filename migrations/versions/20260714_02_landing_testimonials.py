"""landing testimonials table

Revision ID: 20260714_02_landing_testimonials
Revises: 20260714_01_referral_program
Create Date: 2026-07-14 00:30:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260714_02_landing_testimonials"
down_revision = "20260714_01_referral_program"
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


def upgrade():
    bind = op.get_bind()

    if not _has_table(bind, "landing_testimonials"):
        op.create_table(
            "landing_testimonials",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("author_name", sa.String(length=120), nullable=False),
            sa.Column("company_name", sa.String(length=160), nullable=True),
            sa.Column("quote", sa.Text(), nullable=False),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )

    if _has_table(bind, "landing_testimonials") and not _has_index(bind, "landing_testimonials", "ix_landing_testimonials_active"):
        op.create_index("ix_landing_testimonials_active", "landing_testimonials", ["active"])


def downgrade():
    bind = op.get_bind()

    if _has_table(bind, "landing_testimonials") and _has_index(bind, "landing_testimonials", "ix_landing_testimonials_active"):
        op.drop_index("ix_landing_testimonials_active", table_name="landing_testimonials")
    if _has_table(bind, "landing_testimonials"):
        op.drop_table("landing_testimonials")
