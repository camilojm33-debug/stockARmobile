"""Add separate public consent tokens for SaaS prospects."""
from alembic import op
import sqlalchemy as sa

revision = "20261003_01_saas_consent_tokens"
down_revision = "20260927_01_saas_commercial_crm"
branch_labels = None
depends_on = None

def upgrade():
    op.add_column("saas_lead_consents", sa.Column("consent_token", sa.String(96), nullable=True))
    op.create_unique_constraint("uq_saas_lead_consents_consent_token", "saas_lead_consents", ["consent_token"])
    op.create_index("ix_saas_lead_consents_consent_token", "saas_lead_consents", ["consent_token"], unique=False)

def downgrade():
    op.drop_index("ix_saas_lead_consents_consent_token", table_name="saas_lead_consents")
    op.drop_constraint("uq_saas_lead_consents_consent_token", "saas_lead_consents", type_="unique")
    op.drop_column("saas_lead_consents", "consent_token")
