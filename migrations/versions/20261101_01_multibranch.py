"""Introduce Premium multisucursal and migrate existing tenants to Casa central."""
from alembic import op
import sqlalchemy as sa

revision = "20261101_01_multibranch"
down_revision = "20261028_02_pickup"
branch_labels = None
depends_on = None

def _has_table(bind, name):
    return sa.inspect(bind).has_table(name)

def _columns(bind, name):
    return {item["name"] for item in sa.inspect(bind).get_columns(name)} if _has_table(bind, name) else set()

def _add_branch_column(bind, table):
    if not _has_table(bind, table) or "branch_id" in _columns(bind, table):
        return
    op.add_column(table, sa.Column("branch_id", sa.Integer(), sa.ForeignKey("branches.id", ondelete="SET NULL"), nullable=True))
    op.create_index(f"ix_{table}_branch_id", table, ["branch_id"])

def upgrade():
    bind = op.get_bind()
    if not _has_table(bind, "branches"):
        op.create_table("branches",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
            sa.Column("name", sa.String(120), nullable=False), sa.Column("code", sa.String(40), nullable=False),
            sa.Column("address", sa.String(255)), sa.Column("city", sa.String(120)), sa.Column("province", sa.String(120)),
            sa.Column("phone", sa.String(40)), sa.Column("whatsapp", sa.String(40)),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("company_id", "code", name="uq_branches_company_code"))
        op.create_index("ix_branches_company_active", "branches", ["company_id", "active"])
    if not _has_table(bind, "user_branch_access"):
        op.create_table("user_branch_access",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("branch_id", sa.Integer(), sa.ForeignKey("branches.id", ondelete="CASCADE"), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("user_id", "branch_id", name="uq_user_branch_access"))
        op.create_index("ix_user_branch_access_company_user", "user_branch_access", ["company_id", "user_id"])
        op.create_index("ix_user_branch_access_branch", "user_branch_access", ["branch_id"])
    if not _has_table(bind, "branch_stock"):
        op.create_table("branch_stock",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
            sa.Column("branch_id", sa.Integer(), sa.ForeignKey("branches.id", ondelete="CASCADE"), nullable=False),
            sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="CASCADE"), nullable=False),
            sa.Column("stock", sa.Numeric(18, 3), nullable=False, server_default="0"),
            sa.Column("min_stock", sa.Numeric(18, 3), nullable=False, server_default="0"),
            sa.Column("reserved_stock", sa.Numeric(18, 3), nullable=False, server_default="0"),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("branch_id", "product_id", name="uq_branch_stock_branch_product"))
        op.create_index("ix_branch_stock_company_branch", "branch_stock", ["company_id", "branch_id"])
        op.create_index("ix_branch_stock_product", "branch_stock", ["product_id"])
    if not _has_table(bind, "branch_stock_movements"):
        op.create_table("branch_stock_movements",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
            sa.Column("branch_id", sa.Integer(), sa.ForeignKey("branches.id", ondelete="CASCADE"), nullable=False),
            sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="CASCADE"), nullable=False),
            sa.Column("movement_type", sa.String(30), nullable=False), sa.Column("quantity_delta", sa.Numeric(18, 3), nullable=False),
            sa.Column("stock_after", sa.Numeric(18, 3), nullable=False), sa.Column("source_type", sa.String(40)),
            sa.Column("source_id", sa.Integer()), sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
            sa.Column("note", sa.Text()), sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()))
        op.create_index("ix_branch_stock_movements_company_branch_date", "branch_stock_movements", ["company_id", "branch_id", "created_at"])
        op.create_index("ix_branch_stock_movements_product", "branch_stock_movements", ["product_id"])
    for table in ("sales", "purchase_orders", "cash_sessions", "cash_movements", "expenses", "business_documents"):
        _add_branch_column(bind, table)
    if "max_branches" not in _columns(bind, "plans"):
        op.add_column("plans", sa.Column("max_branches", sa.Integer(), nullable=False, server_default="1"))
        bind.execute(sa.text("UPDATE plans SET max_branches = CASE WHEN lower(code) = 'premium' THEN 5 ELSE 1 END"))
    if not _has_table(bind, "companies"):
        return
    companies = bind.execute(sa.text("SELECT id,address,city,province,phone,whatsapp FROM companies ORDER BY id")).mappings().all()
    for company in companies:
        existing = bind.execute(sa.text("SELECT id FROM branches WHERE company_id=:cid AND is_default=TRUE ORDER BY id LIMIT 1"), {"cid":company["id"]}).first()
        if existing:
            branch_id = int(existing[0])
        else:
            branch_id = int(bind.execute(sa.text(
                "INSERT INTO branches(company_id,name,code,address,city,province,phone,whatsapp,active,is_default,created_at,updated_at) "
                "VALUES(:cid,'Casa central','CASA-CENTRAL',:address,:city,:province,:phone,:whatsapp,TRUE,TRUE,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP) RETURNING id"
            ), {"cid":company["id"],"address":company["address"],"city":company["city"],"province":company["province"],"phone":company["phone"],"whatsapp":company["whatsapp"]}).scalar_one())
        for table in ("sales","purchase_orders","cash_sessions","cash_movements","expenses","business_documents","quotes"):
            if _has_table(bind,table) and "branch_id" in _columns(bind,table):
                bind.execute(sa.text(f"UPDATE {table} SET branch_id=:bid WHERE company_id=:cid AND branch_id IS NULL"),{"bid":branch_id,"cid":company["id"]})
        if _has_table(bind,"users"):
            users=bind.execute(sa.text("SELECT id FROM users WHERE company_id=:cid AND role <> 'superadmin'"),{"cid":company["id"]}).all()
            for user in users:
                if not bind.execute(sa.text("SELECT id FROM user_branch_access WHERE user_id=:uid AND branch_id=:bid"),{"uid":user[0],"bid":branch_id}).first():
                    bind.execute(sa.text("INSERT INTO user_branch_access(company_id,user_id,branch_id,created_at) VALUES(:cid,:uid,:bid,CURRENT_TIMESTAMP)"),{"cid":company["id"],"uid":user[0],"bid":branch_id})
        if _has_table(bind,"products"):
            products=bind.execute(sa.text("SELECT id,stock,min_stock FROM products WHERE company_id=:cid"),{"cid":company["id"]}).all()
            for product in products:
                if not bind.execute(sa.text("SELECT id FROM branch_stock WHERE branch_id=:bid AND product_id=:pid"),{"bid":branch_id,"pid":product[0]}).first():
                    bind.execute(sa.text("INSERT INTO branch_stock(company_id,branch_id,product_id,stock,min_stock,reserved_stock,updated_at) VALUES(:cid,:bid,:pid,:stock,:minimum,0,CURRENT_TIMESTAMP)"),{"cid":company["id"],"bid":branch_id,"pid":product[0],"stock":product[1] or 0,"minimum":product[2] or 0})

def downgrade():
    # Preserve branch attribution, inventory balances and movement history.
    pass
