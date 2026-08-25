"""Make tenant-owned dependency foreign keys cascade on tenant deletion.

This complements the direct companies -> tenant tables cascade. It covers
child tables such as sale_items, quote_items, purchase_items and other
records that reference tenant-owned entities without their own company_id.
"""

from alembic import op
import sqlalchemy as sa


revision = "20260825_02_tenant_dependency_cascades"
down_revision = "20260824_01_hard_delete_company_fk_cascade"
branch_labels = None
depends_on = None


def _tenant_tables(bind):
    inspector = sa.inspect(bind)
    tenant_tables = {"companies"}
    for table_name in inspector.get_table_names():
        try:
            columns = {column["name"] for column in inspector.get_columns(table_name)}
        except Exception:
            continue
        if "company_id" in columns:
            tenant_tables.add(table_name)
    return tenant_tables


def _foreign_keys_to_tenant_tables(bind, tenant_tables):
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
            if fk.get("referred_table") not in tenant_tables:
                continue
            name = fk.get("name")
            constrained = fk.get("constrained_columns") or []
            referred = fk.get("referred_columns") or []
            if not name or not constrained or not referred:
                continue
            # Avoid changing self-referential constraints; they do not form
            # part of the tenant root deletion path and may create cycles.
            if table_name == fk.get("referred_table"):
                continue
            result.append((table_name, fk))
    return result


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    tenant_tables = _tenant_tables(bind)
    for table_name, fk in _foreign_keys_to_tenant_tables(bind, tenant_tables):
        options = fk.get("options") or {}
        if (options.get("ondelete") or "").upper() == "CASCADE":
            continue
        op.drop_constraint(fk["name"], table_name, type_="foreignkey")
        op.create_foreign_key(
            fk["name"],
            table_name,
            fk["referred_table"],
            fk["constrained_columns"],
            fk["referred_columns"],
            ondelete="CASCADE",
        )


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    tenant_tables = _tenant_tables(bind)
    for table_name, fk in _foreign_keys_to_tenant_tables(bind, tenant_tables):
        options = fk.get("options") or {}
        if (options.get("ondelete") or "").upper() != "CASCADE":
            continue
        op.drop_constraint(fk["name"], table_name, type_="foreignkey")
        op.create_foreign_key(
            fk["name"],
            table_name,
            fk["referred_table"],
            fk["constrained_columns"],
            fk["referred_columns"],
            ondelete=None,
        )
