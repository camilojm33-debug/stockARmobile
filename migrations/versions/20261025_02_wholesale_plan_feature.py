"""Enable wholesale price lists on the existing Negocio commercial plan."""

from alembic import op
import sqlalchemy as sa

revision = "20261025_02_wholesale_plan"
down_revision = "20261025_01_wholesale_prices"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    row = bind.execute(
        sa.text("SELECT features_json FROM plans WHERE code = :code"),
        {"code": "business"},
    ).fetchone()
    if row is None:
        return

    features = str(row[0] or "")
    tokens = {
        item.strip().lower()
        for item in features.replace(";", ",").split(",")
        if item.strip()
    }
    if "all" in tokens or "wholesale_pricing" in tokens:
        return

    updated = f"{features.strip().rstrip(',')},wholesale_pricing".strip(",")
    bind.execute(
        sa.text("UPDATE plans SET features_json = :features WHERE code = :code"),
        {"features": updated, "code": "business"},
    )


def downgrade():
    # Plan entitlements are admin-managed configuration; preserve any later edits.
    pass
