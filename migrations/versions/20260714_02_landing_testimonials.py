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


def upgrade():
    op.create_table(
        "landing_testimonials",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("author_name", sa.String(length=120), nullable=False),
        sa.Column("company_name", sa.String(length=160), nullable=True),
        sa.Column("quote", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_landing_testimonials_active", "landing_testimonials", ["active"])


def downgrade():
    op.drop_index("ix_landing_testimonials_active", table_name="landing_testimonials")
    op.drop_table("landing_testimonials")
