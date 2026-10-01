"""Allow deleting CRM prospects without deleting commercial checkout history."""

from alembic import op
import sqlalchemy as sa

revision = "20261001_06_saas_lead_delete"
down_revision = "20261001_05_commercial_campaign_multichannel"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("saas_commercial_checkouts", schema=None) as batch_op:
        batch_op.alter_column("lead_id", existing_type=sa.Integer(), nullable=True)
        batch_op.drop_constraint(
            "saas_commercial_checkouts_lead_id_fkey",
            type_="foreignkey",
        )
        batch_op.create_foreign_key(
            "saas_commercial_checkouts_lead_id_fkey",
            "saas_leads",
            ["lead_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade():
    bind = op.get_bind()
    remaining = bind.execute(
        sa.text(
            "SELECT count(*) FROM saas_commercial_checkouts "
            "WHERE lead_id IS NULL"
        )
    ).scalar()
    if int(remaining or 0):
        raise RuntimeError(
            "No se puede revertir: existen checkouts comerciales desvinculados de un prospecto."
        )

    with op.batch_alter_table("saas_commercial_checkouts", schema=None) as batch_op:
        batch_op.drop_constraint(
            "saas_commercial_checkouts_lead_id_fkey",
            type_="foreignkey",
        )
        batch_op.create_foreign_key(
            "saas_commercial_checkouts_lead_id_fkey",
            "saas_leads",
            ["lead_id"],
            ["id"],
        )
        batch_op.alter_column("lead_id", existing_type=sa.Integer(), nullable=False)
