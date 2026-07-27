"""merge crm and quotes heads

Revision ID: 20260727_02_merge_crm_and_quotes_heads
Revises: 20260723_01_saas_crm_center, 20260727_01_quotes_module
Create Date: 2026-07-27 00:30:00.000000
"""

from alembic import op


revision = "20260727_02_merge_crm_and_quotes_heads"
down_revision = ("20260723_01_saas_crm_center", "20260727_01_quotes_module")
branch_labels = None
depends_on = None


def upgrade():
    # Merge revision only: no schema changes.
    pass


def downgrade():
    # Splits back into two heads when downgrading below this revision.
    pass
