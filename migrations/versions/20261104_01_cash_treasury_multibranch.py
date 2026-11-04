"""Add physical cash registers, auditable session events and branch treasury.

The migration is additive. Existing cash sessions are assigned to a default
register per branch; simultaneous legacy open sessions receive their own
compatibility register so their active work is not invalidated.
"""
from alembic import op
import sqlalchemy as sa


revision = "20261104_01_cash_treasury_multibranch"
down_revision = "20261102_01_multibranch_opening"
branch_labels = None
depends_on = None


def _has_table(bind, name):
    return sa.inspect(bind).has_table(name)


def _columns(bind, name):
    return {item["name"] for item in sa.inspect(bind).get_columns(name)} if _has_table(bind, name) else set()


def _indexes(bind, table):
    return {item.get("name") for item in sa.inspect(bind).get_indexes(table)} if _has_table(bind, table) else set()


def upgrade():
    bind = op.get_bind()

    if not _has_table(bind, "cash_registers"):
        op.create_table(
            "cash_registers",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
            sa.Column("branch_id", sa.Integer(), sa.ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("name", sa.String(100), nullable=False),
            sa.Column("code", sa.String(40), nullable=False),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("assigned_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
            sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("company_id", "branch_id", "code", name="uq_cash_register_company_branch_code"),
        )
    if "ix_cash_registers_company_id" not in _indexes(bind, "cash_registers"):
        op.create_index("ix_cash_registers_company_id", "cash_registers", ["company_id"])
    if "ix_cash_registers_branch_id" not in _indexes(bind, "cash_registers"):
        op.create_index("ix_cash_registers_branch_id", "cash_registers", ["branch_id"])
    if "ix_cash_registers_active" not in _indexes(bind, "cash_registers"):
        op.create_index("ix_cash_registers_active", "cash_registers", ["active"])
    if "ix_cash_registers_assigned_user_id" not in _indexes(bind, "cash_registers"):
        op.create_index("ix_cash_registers_assigned_user_id", "cash_registers", ["assigned_user_id"])
    if "ix_cash_registers_company_branch_active" not in _indexes(bind, "cash_registers"):
        op.create_index("ix_cash_registers_company_branch_active", "cash_registers", ["company_id", "branch_id", "active"])

    if not _has_table(bind, "cash_session_events"):
        op.create_table(
            "cash_session_events",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
            sa.Column("branch_id", sa.Integer(), sa.ForeignKey("branches.id", ondelete="SET NULL")),
            sa.Column("cash_session_id", sa.Integer(), sa.ForeignKey("cash_sessions.id", ondelete="CASCADE"), nullable=False),
            sa.Column("event_type", sa.String(30), nullable=False),
            sa.Column("actor_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
            sa.Column("opening_amount", sa.Numeric(14, 2)),
            sa.Column("expected_amount", sa.Numeric(14, 2)),
            sa.Column("counted_amount", sa.Numeric(14, 2)),
            sa.Column("difference_amount", sa.Numeric(14, 2)),
            sa.Column("note", sa.Text()),
            sa.Column("payload_json", sa.Text()),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )
        op.create_index("ix_cash_session_events_company_id", "cash_session_events", ["company_id"])
        op.create_index("ix_cash_session_events_branch_id", "cash_session_events", ["branch_id"])
        op.create_index("ix_cash_session_events_cash_session_id", "cash_session_events", ["cash_session_id"])
        op.create_index("ix_cash_session_events_event_type", "cash_session_events", ["event_type"])
        op.create_index("ix_cash_session_events_created_at", "cash_session_events", ["created_at"])
        op.create_index("ix_cash_session_events_session_created", "cash_session_events", ["cash_session_id", "created_at"])

    if not _has_table(bind, "cash_transfers"):
        op.create_table(
            "cash_transfers",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
            sa.Column("source_branch_id", sa.Integer(), sa.ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("target_branch_id", sa.Integer(), sa.ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("source_register_id", sa.Integer(), sa.ForeignKey("cash_registers.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("target_register_id", sa.Integer(), sa.ForeignKey("cash_registers.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("source_session_id", sa.Integer(), sa.ForeignKey("cash_sessions.id", ondelete="RESTRICT")),
            sa.Column("amount", sa.Numeric(14, 2), nullable=False),
            sa.Column("currency", sa.String(10), nullable=False, server_default="ARS"),
            sa.Column("status", sa.String(20), nullable=False, server_default="pendiente"),
            sa.Column("idempotency_key", sa.String(100), nullable=False),
            sa.Column("description", sa.String(255)),
            sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
            sa.Column("dispatched_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
            sa.Column("received_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
            sa.Column("rejected_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("dispatched_at", sa.DateTime()),
            sa.Column("received_at", sa.DateTime()),
            sa.Column("rejected_at", sa.DateTime()),
            sa.UniqueConstraint("company_id", "idempotency_key", name="uq_cash_transfers_company_idempotency"),
        )
        for name, columns in (
            ("ix_cash_transfers_company_id", ["company_id"]),
            ("ix_cash_transfers_source_branch_id", ["source_branch_id"]),
            ("ix_cash_transfers_target_branch_id", ["target_branch_id"]),
            ("ix_cash_transfers_source_register_id", ["source_register_id"]),
            ("ix_cash_transfers_target_register_id", ["target_register_id"]),
            ("ix_cash_transfers_source_session_id", ["source_session_id"]),
            ("ix_cash_transfers_status", ["status"]),
            ("ix_cash_transfers_created_at", ["created_at"]),
            ("ix_cash_transfers_company_status_created", ["company_id", "status", "created_at"]),
            ("ix_cash_transfers_source_branch", ["source_branch_id", "status"]),
            ("ix_cash_transfers_target_branch", ["target_branch_id", "status"]),
        ):
            op.create_index(name, "cash_transfers", columns)

    if not _has_table(bind, "financial_accounts"):
        op.create_table(
            "financial_accounts",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
            sa.Column("branch_id", sa.Integer(), sa.ForeignKey("branches.id", ondelete="SET NULL")),
            sa.Column("name", sa.String(120), nullable=False),
            sa.Column("code", sa.String(40), nullable=False),
            sa.Column("account_type", sa.String(30), nullable=False, server_default="banco"),
            sa.Column("provider", sa.String(80)),
            sa.Column("currency", sa.String(10), nullable=False, server_default="ARS"),
            sa.Column("opening_balance", sa.Numeric(14, 2), nullable=False, server_default="0"),
            sa.Column("alias", sa.String(120)),
            sa.Column("account_reference", sa.String(120)),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("company_id", "branch_id", "code", name="uq_financial_accounts_company_branch_code"),
        )
        op.create_index("ix_financial_accounts_company_id", "financial_accounts", ["company_id"])
        op.create_index("ix_financial_accounts_branch_id", "financial_accounts", ["branch_id"])
        op.create_index("ix_financial_accounts_active", "financial_accounts", ["active"])
        op.create_index("ix_financial_accounts_created_by_user_id", "financial_accounts", ["created_by_user_id"])
        op.create_index("ix_financial_accounts_company_branch_active", "financial_accounts", ["company_id", "branch_id", "active"])

    if not _has_table(bind, "financial_account_movements"):
        op.create_table(
            "financial_account_movements",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
            sa.Column("branch_id", sa.Integer(), sa.ForeignKey("branches.id", ondelete="SET NULL")),
            sa.Column("account_id", sa.Integer(), sa.ForeignKey("financial_accounts.id", ondelete="CASCADE"), nullable=False),
            sa.Column("movement_type", sa.String(20), nullable=False),
            sa.Column("amount", sa.Numeric(14, 2), nullable=False),
            sa.Column("description", sa.String(255)),
            sa.Column("reference", sa.String(120)),
            sa.Column("source_type", sa.String(40)),
            sa.Column("source_id", sa.Integer()),
            sa.Column("idempotency_key", sa.String(100)),
            sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("company_id", "idempotency_key", name="uq_financial_account_movements_company_idempotency"),
        )
        op.create_index("ix_financial_account_movements_company_id", "financial_account_movements", ["company_id"])
        op.create_index("ix_financial_account_movements_branch_id", "financial_account_movements", ["branch_id"])
        op.create_index("ix_financial_account_movements_account_id", "financial_account_movements", ["account_id"])
        op.create_index("ix_financial_account_movements_created_by_user_id", "financial_account_movements", ["created_by_user_id"])
        op.create_index("ix_financial_account_movements_created_at", "financial_account_movements", ["created_at"])
        op.create_index("ix_financial_account_movements_account_created", "financial_account_movements", ["account_id", "created_at"])
        op.create_index("ix_financial_account_movements_company_branch_created", "financial_account_movements", ["company_id", "branch_id", "created_at"])

    # Add the new nullable relationships without changing existing financial history.
    if "cash_register_id" not in _columns(bind, "cash_sessions"):
        op.add_column("cash_sessions", sa.Column("cash_register_id", sa.Integer(), nullable=True))
        op.create_foreign_key(
            "fk_cash_sessions_cash_register_id", "cash_sessions", "cash_registers",
            ["cash_register_id"], ["id"], ondelete="SET NULL",
        )
        op.create_index("ix_cash_sessions_cash_register_id", "cash_sessions", ["cash_register_id"])

    if "cash_transfer_id" not in _columns(bind, "cash_movements"):
        op.add_column("cash_movements", sa.Column("cash_transfer_id", sa.Integer(), nullable=True))
        op.create_foreign_key(
            "fk_cash_movements_cash_transfer_id", "cash_movements", "cash_transfers",
            ["cash_transfer_id"], ["id"], ondelete="SET NULL",
        )
        op.create_index("ix_cash_movements_cash_transfer_id", "cash_movements", ["cash_transfer_id"])
        op.create_unique_constraint(
            "uq_cash_movements_transfer_type", "cash_movements", ["cash_transfer_id", "movement_type"]
        )

    if "cash_session_id" not in _columns(bind, "expenses"):
        op.add_column("expenses", sa.Column("cash_session_id", sa.Integer(), nullable=True))
        op.create_foreign_key(
            "fk_expenses_cash_session_id", "expenses", "cash_sessions",
            ["cash_session_id"], ["id"], ondelete="SET NULL",
        )
        op.create_index("ix_expenses_cash_session_id", "expenses", ["cash_session_id"])

    # Create one default physical register for every existing branch.
    branches = bind.execute(
        sa.text("SELECT id,company_id FROM branches ORDER BY company_id,id")
    ).mappings().all()
    default_register_by_branch = {}
    for branch in branches:
        existing = bind.execute(
            sa.text(
                "SELECT id FROM cash_registers WHERE company_id=:company_id "
                "AND branch_id=:branch_id AND is_default=TRUE ORDER BY id LIMIT 1"
            ),
            {"company_id": branch["company_id"], "branch_id": branch["id"]},
        ).first()
        if existing:
            register_id = int(existing[0])
        else:
            register_id = int(
                bind.execute(
                    sa.text(
                        "INSERT INTO cash_registers(company_id,branch_id,name,code,active,is_default,created_at,updated_at) "
                        "VALUES(:company_id,:branch_id,'Caja 1','CAJA-1',TRUE,TRUE,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP) "
                        "RETURNING id"
                    ),
                    {"company_id": branch["company_id"], "branch_id": branch["id"]},
                ).scalar_one()
            )
        default_register_by_branch[int(branch["id"])] = register_id

    # Closed sessions can share the default register. Each simultaneously open
    # legacy session gets a compatibility register to preserve parallel shifts.
    sessions = bind.execute(
        sa.text(
            "SELECT id,company_id,branch_id,status FROM cash_sessions "
            "WHERE cash_register_id IS NULL AND branch_id IS NOT NULL ORDER BY branch_id,id"
        )
    ).mappings().all()
    first_open_by_branch = set()
    for session in sessions:
        branch_id = int(session["branch_id"])
        default_id = default_register_by_branch.get(branch_id)
        if default_id is None:
            continue
        register_id = default_id
        if str(session["status"] or "").lower() == "abierta":
            if branch_id in first_open_by_branch:
                register_id = int(
                    bind.execute(
                        sa.text(
                            "INSERT INTO cash_registers(company_id,branch_id,name,code,active,is_default,created_at,updated_at) "
                            "VALUES(:company_id,:branch_id,:name,:code,TRUE,FALSE,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP) "
                            "RETURNING id"
                        ),
                        {
                            "company_id": session["company_id"],
                            "branch_id": branch_id,
                            "name": f"Caja migrada #{session['id']}",
                            "code": f"LEGACY-{session['id']}",
                        },
                    ).scalar_one()
                )
            else:
                first_open_by_branch.add(branch_id)
        bind.execute(
            sa.text("UPDATE cash_sessions SET cash_register_id=:register_id WHERE id=:session_id"),
            {"register_id": register_id, "session_id": session["id"]},
        )

    # Link previous cash-expense movements to their parent expense where the
    # historic description contains the expense ID generated by expenses.py.
    if "cash_session_id" in _columns(bind, "expenses"):
        bind.execute(
            sa.text(
                "UPDATE expenses SET cash_session_id=("
                "SELECT cm.session_id FROM cash_movements cm "
                "WHERE cm.company_id=expenses.company_id AND cm.branch_id=expenses.branch_id "
                "AND cm.category='gasto' AND cm.description LIKE ('Gasto #' || CAST(expenses.id AS VARCHAR) || ':%') "
                "ORDER BY cm.id DESC LIMIT 1"
                ") WHERE cash_session_id IS NULL"
            )
        )

    # Only one open shift may occupy a physical drawer at a time.
    if "uq_cash_sessions_open_register" not in _indexes(bind, "cash_sessions"):
        op.create_index(
            "uq_cash_sessions_open_register",
            "cash_sessions",
            ["cash_register_id"],
            unique=True,
            postgresql_where=sa.text("status = 'abierta' AND cash_register_id IS NOT NULL"),
            sqlite_where=sa.text("status = 'abierta' AND cash_register_id IS NOT NULL"),
        )


def downgrade():
    # Keep the financial audit trail and branch attribution on downgrade.
    pass
