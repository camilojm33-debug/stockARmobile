"""Store the pending commercial activation secret encrypted at rest."""
from alembic import op
import sqlalchemy as sa

revision = "20261001_04_commercial_activation_secret"
down_revision = "20261001_03_commercial_checkout"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("saas_commercial_checkouts", sa.Column("activation_token_encrypted", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("saas_commercial_checkouts", "activation_token_encrypted")
