"""merge referral and password heads

Revision ID: 8a0e95fc2189
Revises: 20260714_05_enforce_users_must_change_password, 20260714_06_referral_seller_cvu
Create Date: 2026-07-14 20:42:38.981827

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '8a0e95fc2189'
down_revision = ('20260714_05_enforce_users_must_change_password', '20260714_06_referral_seller_cvu')
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
