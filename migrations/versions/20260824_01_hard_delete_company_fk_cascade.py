"""make tenant foreign keys cascade on company deletion

Revision ID: 20260824_01_hard_delete_company_fk_cascade
Revises: 20260729_01_subscription_commands_hardening
Create Date: 2026-08-24 21:55:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260824_01_hard_delete_company_fk_cascade"
down_revision = "20260729_01_subscription_commands_hardening"
branch_labels = None
depends_on = None


def _company_foreign_keys(bind):
    inspector = sa.inspect(bind)
    result = []
    for table_name in inspector.get_table_names():
        if table_name == "companies":
            continue
        try:
            foreign_keys = inspector.get_foreign_keys(table_name) or []
        except Exception:
            continue
        for fk in foreign_keys:
            if fk.get("referred_table") != "companies":
                continue
            constrained_columns = fk.get("constrained_columns") or []
            referred_columns = fk.get("referred_columns") or []
            name = fk.get("name")
            if name and constrained_columns and referred_columns:
                result.append(
                    {
                        "table": table_name,
                        "name": name,
                        "columns": constrained_columns,
                        "referred_columns": referred_columns,
                        "options": fk.get("options") or {},
                    }
                )
    return result


def upgrade():
    bind = op.get_bind()
    dialect = bind.dialect.name

    # Production uses PostgreSQL. SQLite cannot safely rewrite arbitrary existing
    # foreign-key constraints with ALTER TABLE, so leave its schema untouched.
    if dialect != "postgresql":
        return

    for fk in _company_foreign_keys(bind):
        options = fk["options"]
        if (options.get("ondelete") or "").upper() == "CASCADE":
            continue

        op.drop_constraint(fk["name"], fk["table"], type_="foreignkey")
        op.create_foreign_key(
            fk["name"],
            fk["table"],
            "companies",
            fk["columns"],
            fk["referred_columns"],
            ondelete="CASCADE",
        )


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    for fk in _company_foreign_keys(bind):
        options = fk["options"]
        if (options.get("ondelete") or "").upper() != "CASCADE":
            continue

        op.drop_constraint(fk["name"], fk["table"], type_="foreignkey")
        original_ondelete = options.get("ondelete")
        # Recreate the constraint without CASCADE. This matches the original
        # schema behavior for constraints that had no ON DELETE action.
        op.create_foreign_key(
            fk["name"],
            fk["table"],
            "companies",
            fk["columns"],
            fk["referred_columns"],
            ondelete=None if not original_ondelete or original_ondelete.upper() == "CASCADE" else original_ondelete,
        )
