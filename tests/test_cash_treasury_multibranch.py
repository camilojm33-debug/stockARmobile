from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from app import (
    Branch,
    CashMovement,
    CashRegister,
    CashSession,
    CashSessionEvent,
    CashTransfer,
    Company,
    Expense,
    FinancialAccount,
    FinancialAccountMovement,
    User,
    db,
)
from cash import _session_summary
from cash_treasury import financial_account_balance
from services.backup_service import BackupService
from stockarmobile.permissions import EMPLOYEE_ADMIN_ONLY, employee_endpoint_permission


def _company(name):
    company = Company(name=name, active=True)
    db.session.add(company)
    db.session.flush()
    return company


def _branch(company, name, code, *, default=False):
    branch = Branch(
        company_id=company.id,
        name=name,
        code=code,
        active=True,
        is_default=default,
    )
    db.session.add(branch)
    db.session.flush()
    return branch


def _admin(company, prefix="cash_treasury"):
    user = User(
        username=f"{prefix}_admin",
        email=f"{prefix}_admin@example.test",
        password_hash="unused-test-hash",
        role="admin",
        active=True,
        company_id=company.id,
    )
    db.session.add(user)
    db.session.flush()
    return user


def _login(test_client, user):
    with test_client.session_transaction() as session:
        session["_user_id"] = str(user.id)
        session["_fresh"] = True


def test_cash_registers_are_branch_scoped_and_only_one_open_session_occupies_a_register(app):
    with app.app_context():
        company = _company("Empresa cajas independientes")
        central = _branch(company, "Casa central", "CENTRAL", default=True)
        north = _branch(company, "Sucursal Norte", "NORTE")
        user = _admin(company, "cash_register")

        central_register = CashRegister(
            company_id=company.id, branch_id=central.id, name="Caja 1",
            code="CAJA-1", active=True, is_default=True,
        )
        central_second = CashRegister(
            company_id=company.id, branch_id=central.id, name="Caja 2",
            code="CAJA-2", active=True,
        )
        north_register = CashRegister(
            company_id=company.id, branch_id=north.id, name="Caja 1",
            code="CAJA-1", active=True, is_default=True,
        )
        db.session.add_all([central_register, central_second, north_register])
        db.session.flush()

        db.session.add_all([
            CashSession(
                company_id=company.id, branch_id=central.id, cash_register_id=central_register.id,
                user_id=user.id, opened_by_user_id=user.id, opening_amount=Decimal("100"),
                expected_amount=Decimal("100"), status="abierta",
            ),
            CashSession(
                company_id=company.id, branch_id=central.id, cash_register_id=central_second.id,
                user_id=user.id, opened_by_user_id=user.id, opening_amount=Decimal("50"),
                expected_amount=Decimal("50"), status="abierta",
            ),
            CashSession(
                company_id=company.id, branch_id=north.id, cash_register_id=north_register.id,
                user_id=user.id, opened_by_user_id=user.id, opening_amount=Decimal("25"),
                expected_amount=Decimal("25"), status="abierta",
            ),
        ])
        db.session.commit()

        assert CashSession.query.filter_by(company_id=company.id, status="abierta").count() == 3

        duplicate = CashSession(
            company_id=company.id, branch_id=central.id, cash_register_id=central_register.id,
            user_id=user.id, opened_by_user_id=user.id, opening_amount=0, status="abierta",
        )
        db.session.add(duplicate)
        with pytest.raises(IntegrityError):
            db.session.flush()
        db.session.rollback()

        assert CashSession.query.filter_by(
            company_id=company.id, cash_register_id=central_register.id, status="abierta"
        ).count() == 1


def test_cash_transfer_lifecycle_deducts_and_adds_cash_exactly_once(app):
    with app.app_context():
        company = _company("Empresa transferencias")
        source_branch = _branch(company, "Resistencia", "RES")
        target_branch = _branch(company, "Corrientes", "COR")
        user = _admin(company, "cash_transfer")

        source_register = CashRegister(
            company_id=company.id, branch_id=source_branch.id, name="Caja 1",
            code="RES-1", active=True, is_default=True,
        )
        target_register = CashRegister(
            company_id=company.id, branch_id=target_branch.id, name="Caja 1",
            code="COR-1", active=True, is_default=True,
        )
        db.session.add_all([source_register, target_register])
        db.session.flush()

        source_session = CashSession(
            company_id=company.id, branch_id=source_branch.id,
            cash_register_id=source_register.id, user_id=user.id,
            opened_by_user_id=user.id, opening_amount=Decimal("120"),
            expected_amount=Decimal("120"), status="abierta",
        )
        target_session = CashSession(
            company_id=company.id, branch_id=target_branch.id,
            cash_register_id=target_register.id, user_id=user.id,
            opened_by_user_id=user.id, opening_amount=Decimal("5"),
            expected_amount=Decimal("5"), status="abierta",
        )
        db.session.add_all([source_session, target_session])
        db.session.commit()

        source_session_id = source_session.id
        target_session_id = target_session.id
        target_branch_id = target_branch.id
        target_register_id = target_register.id
        test_client = app.test_client()
        _login(test_client, user)

        created = test_client.post(
            "/caja/tesoreria/transferencias/crear",
            data={
                "source_session_id": str(source_session_id),
                "target_branch_id": str(target_branch_id),
                "target_register_id": str(target_register_id),
                "amount": "40.00",
                "description": "Reposición de efectivo",
                "idempotency_key": "cash-transfer-qa-001",
            },
            follow_redirects=False,
        )
        assert created.status_code == 302
        transfer = CashTransfer.query.filter_by(
            company_id=company.id, idempotency_key="cash-transfer-qa-001"
        ).one()
        transfer_id = transfer.id
        assert transfer.status == "pendiente"
        assert CashMovement.query.filter_by(cash_transfer_id=transfer_id).count() == 0

        # Repeating the request with the same idempotency key does not create
        # a second transfer request or a cash movement.
        test_client.post(
            "/caja/tesoreria/transferencias/crear",
            data={
                "source_session_id": str(source_session_id),
                "target_branch_id": str(target_branch_id),
                "target_register_id": str(target_register_id),
                "amount": "40.00",
                "idempotency_key": "cash-transfer-qa-001",
            },
        )
        assert CashTransfer.query.filter_by(company_id=company.id).count() == 1

        dispatched = test_client.post(
            f"/caja/tesoreria/transferencias/{transfer_id}/despachar",
            follow_redirects=False,
        )
        assert dispatched.status_code == 302
        db.session.expire_all()
        transfer = db.session.get(CashTransfer, transfer_id)
        assert transfer.status == "en_transito"
        outgoing = CashMovement.query.filter_by(
            cash_transfer_id=transfer_id, movement_type="retiro"
        ).one()
        assert Decimal(str(outgoing.amount)) == Decimal("40.00")

        received = test_client.post(
            f"/caja/tesoreria/transferencias/{transfer_id}/recibir",
            follow_redirects=False,
        )
        assert received.status_code == 302
        db.session.expire_all()
        transfer = db.session.get(CashTransfer, transfer_id)
        assert transfer.status == "recibida"

        incoming = CashMovement.query.filter_by(
            cash_transfer_id=transfer_id, movement_type="ingreso"
        ).one()
        assert Decimal(str(incoming.amount)) == Decimal("40.00")
        assert CashMovement.query.filter_by(cash_transfer_id=transfer_id).count() == 2

        # A duplicate receipt is rejected by state transition and the unique
        # transfer/movement-type key remains a second line of defense.
        test_client.post(
            f"/caja/tesoreria/transferencias/{transfer_id}/recibir",
            follow_redirects=False,
        )
        assert CashMovement.query.filter_by(
            cash_transfer_id=transfer_id, movement_type="ingreso"
        ).count() == 1

        db.session.expire_all()
        source = db.session.get(CashSession, source_session_id)
        target = db.session.get(CashSession, target_session_id)
        assert _session_summary(source)["expected"] == Decimal("80.00")
        assert _session_summary(target)["expected"] == Decimal("45.00")


def test_financial_account_ledger_uses_signed_movements_for_balance(app):
    with app.app_context():
        company = _company("Empresa cuentas financieras")
        branch = _branch(company, "Casa central", "CENTRAL")
        user = _admin(company, "financial_account")
        account = FinancialAccount(
            company_id=company.id,
            branch_id=branch.id,
            name="Mercado Pago",
            code="MP-PRINCIPAL",
            account_type="billetera",
            provider="Mercado Pago",
            opening_balance=Decimal("1000.00"),
            active=True,
            created_by_user_id=user.id,
        )
        db.session.add(account)
        db.session.flush()
        db.session.add_all([
            FinancialAccountMovement(
                company_id=company.id, branch_id=branch.id, account=account,
                movement_type="ingreso", amount=Decimal("250.00"),
                idempotency_key="mp-income-1", created_by_user_id=user.id,
            ),
            FinancialAccountMovement(
                company_id=company.id, branch_id=branch.id, account=account,
                movement_type="egreso", amount=Decimal("100.00"),
                idempotency_key="mp-expense-1", created_by_user_id=user.id,
            ),
            FinancialAccountMovement(
                company_id=company.id, branch_id=branch.id, account=account,
                movement_type="ajuste", amount=Decimal("-25.00"),
                idempotency_key="mp-adjust-1", created_by_user_id=user.id,
            ),
        ])
        db.session.flush()

        assert financial_account_balance(account) == Decimal("1125.00")
        db.session.rollback()


def test_cash_expense_requires_an_open_cash_session(app):
    with app.app_context():
        company = _company("Empresa gasto sin caja")
        user = _admin(company, "cash_expense_guard")
        db.session.commit()

        test_client = app.test_client()
        _login(test_client, user)
        response = test_client.post(
            "/gastos/",
            data={
                "category": "Combustible",
                "description": "Pago efectivo sin caja",
                "amount": "25.00",
                "payment_method": "EFECTIVO",
            },
            follow_redirects=False,
        )

        assert response.status_code == 302
        assert Expense.query.filter_by(company_id=company.id).count() == 0


def test_reopening_a_closed_session_creates_a_new_shift_and_preserves_old_close(app):
    with app.app_context():
        company = _company("Empresa reapertura segura")
        user = _admin(company, "cash_reopen")
        old_session = CashSession(
            company_id=company.id,
            user_id=user.id,
            opened_by_user_id=user.id,
            opening_amount=Decimal("100.00"),
            expected_amount=Decimal("150.00"),
            counted_amount=Decimal("150.00"),
            closing_amount=Decimal("150.00"),
            difference_amount=Decimal("0.00"),
            status="cerrada",
            closing_note="Arqueo de fin de turno",
        )
        db.session.add(old_session)
        db.session.commit()
        old_session_id = old_session.id

        test_client = app.test_client()
        _login(test_client, user)
        response = test_client.post(
            "/caja/",
            data={
                "action": "reopen",
                "session_id": str(old_session_id),
                "reopen_reason": "Nuevo turno autorizado",
            },
            follow_redirects=False,
        )
        assert response.status_code == 302

        db.session.expire_all()
        old_session = db.session.get(CashSession, old_session_id)
        assert old_session.status == "cerrada"
        assert old_session.closing_amount == Decimal("150.00")
        assert old_session.counted_amount == Decimal("150.00")

        new_session = CashSession.query.filter_by(
            company_id=company.id,
            user_id=user.id,
            status="abierta",
        ).one()
        assert new_session.id != old_session_id
        assert new_session.opening_amount == Decimal("150.00")
        assert CashSessionEvent.query.filter_by(
            cash_session_id=old_session_id, event_type="reapertura_solicitada"
        ).count() == 1
        assert CashSessionEvent.query.filter_by(
            cash_session_id=new_session.id, event_type="apertura_reapertura"
        ).count() == 1


def test_treasury_routes_are_admin_only_for_employee_permission_matrix():
    assert employee_endpoint_permission("cash_treasury.index", "GET") == EMPLOYEE_ADMIN_ONLY
    assert employee_endpoint_permission("cash_treasury.receive_transfer", "POST") == EMPLOYEE_ADMIN_ONLY


def test_treasury_dashboard_renders_branch_summary_and_financial_accounts(app):
    with app.app_context():
        company = _company("Empresa panel tesoreria")
        branch = _branch(company, "Casa central", "CENTRAL", default=True)
        user = _admin(company, "cash_dashboard")
        register = CashRegister(
            company_id=company.id, branch_id=branch.id, name="Caja 1",
            code="CENTRAL-1", active=True, is_default=True,
        )
        db.session.add(register)
        db.session.flush()
        cash_session = CashSession(
            company_id=company.id,
            branch_id=branch.id,
            cash_register_id=register.id,
            user_id=user.id,
            opened_by_user_id=user.id,
            opening_amount=Decimal("200.00"),
            expected_amount=Decimal("200.00"),
            status="abierta",
        )
        account = FinancialAccount(
            company_id=company.id,
            branch_id=branch.id,
            name="Banco",
            code="BANCO-1",
            account_type="banco",
            opening_balance=Decimal("500.00"),
            active=True,
        )
        db.session.add_all([cash_session, account])
        db.session.commit()

        test_client = app.test_client()
        _login(test_client, user)
        response = test_client.get("/caja/tesoreria/")
        assert response.status_code == 200
        assert b"Caja y Tesorer" in response.data
        assert b"Casa central" in response.data
        assert b"Banco" in response.data


def test_full_backup_contains_cash_treasury_ledgers_and_validates_their_links(app):
    with app.app_context():
        company = _company("Empresa backup tesoreria")
        source_branch = _branch(company, "Casa central", "CENTRAL", default=True)
        target_branch = _branch(company, "Sucursal Norte", "NORTE")
        user = _admin(company, "cash_backup")
        source_register = CashRegister(
            company_id=company.id, branch_id=source_branch.id, name="Caja 1",
            code="CENTRAL-1", active=True, is_default=True,
        )
        target_register = CashRegister(
            company_id=company.id, branch_id=target_branch.id, name="Caja 1",
            code="NORTE-1", active=True, is_default=True,
        )
        db.session.add_all([source_register, target_register])
        db.session.flush()
        source_session = CashSession(
            company_id=company.id, branch_id=source_branch.id,
            cash_register_id=source_register.id, user_id=user.id,
            opened_by_user_id=user.id, opening_amount=Decimal("75.00"),
            expected_amount=Decimal("75.00"), status="abierta",
        )
        account = FinancialAccount(
            company_id=company.id, branch_id=source_branch.id,
            name="Banco central", code="BANCO-1", account_type="banco",
            opening_balance=Decimal("900.00"), active=True,
        )
        db.session.add_all([source_session, account])
        db.session.flush()
        transfer = CashTransfer(
            company_id=company.id,
            source_branch_id=source_branch.id,
            target_branch_id=target_branch.id,
            source_register_id=source_register.id,
            target_register_id=target_register.id,
            source_session_id=source_session.id,
            amount=Decimal("20.00"),
            status="pendiente",
            idempotency_key="backup-cash-transfer-1",
            created_by_user_id=user.id,
        )
        db.session.add(transfer)
        db.session.flush()
        db.session.add_all([
            CashSessionEvent(
                company_id=company.id, branch_id=source_branch.id,
                cash_session_id=source_session.id, event_type="apertura",
                actor_user_id=user.id, opening_amount=Decimal("75.00"),
                expected_amount=Decimal("75.00"),
            ),
            FinancialAccountMovement(
                company_id=company.id, branch_id=source_branch.id,
                account_id=account.id, movement_type="ingreso",
                amount=Decimal("30.00"), idempotency_key="backup-ledger-1",
                created_by_user_id=user.id,
            ),
            CashMovement(
                company_id=company.id, branch_id=source_branch.id,
                session_id=source_session.id, user_id=user.id,
                cash_transfer_id=transfer.id, movement_type="retiro",
                category="transferencia_enviada", amount=Decimal("20.00"),
                description="Transferencia de prueba",
            ),
            Expense(
                company_id=company.id, branch_id=source_branch.id,
                cash_session_id=source_session.id, user_id=user.id,
                category="Otros", description="Gasto en caja",
                amount=Decimal("5.00"), payment_method="EFECTIVO",
            ),
        ])
        db.session.commit()

        payload = BackupService._payload_for_company(company.id)
        assert len(payload["cash_registers"]) == 2
        assert len(payload["cash_session_events"]) == 1
        assert len(payload["cash_transfers"]) == 1
        assert len(payload["financial_accounts"]) == 1
        assert len(payload["financial_account_movements"]) == 1
        BackupService._validate_backup_tenant_scope(payload, company.id)

        backup, _plan = BackupService.create_manual_backup(
            company.id,
            user_id=user.id,
            trigger_type="cash_treasury_qa",
        )
        db.session.commit()
        verification = BackupService.verify_restore_round_trip(
            backup,
            expected_company_id=company.id,
        )
        assert verification["valid"] is True
        assert verification["rolled_back"] is True


def test_sale_posts_mapped_digital_payment_to_the_branch_account_once(app):
    from types import SimpleNamespace
    from cash_treasury import record_sale_financial_movements

    with app.app_context():
        company = _company("Empresa ledger automático de ventas")
        branch = _branch(company, "Casa central", "CENTRAL", default=True)
        user = _admin(company, "cash_sale_ledger")
        central_account = FinancialAccount(
            company_id=company.id,
            branch_id=None,
            name="Mercado Pago central",
            code="MP-CENTRAL",
            account_type="billetera",
            payment_method_key="mercado_pago",
            opening_balance=Decimal("100.00"),
            active=True,
        )
        branch_account = FinancialAccount(
            company_id=company.id,
            branch_id=branch.id,
            name="Mercado Pago sucursal",
            code="MP-SUCURSAL",
            account_type="billetera",
            payment_method_key="mercado_pago",
            opening_balance=Decimal("20.00"),
            active=True,
        )
        db.session.add_all([central_account, branch_account])
        db.session.flush()

        sale = SimpleNamespace(
            id=98765,
            company_id=company.id,
            branch_id=branch.id,
            seller_id=user.id,
        )
        posted = record_sale_financial_movements(
            sale,
            {"efectivo": Decimal("10.00"), "mercado_pago": Decimal("85.50")},
        )
        assert len(posted) == 1
        assert posted[0].account_id == branch_account.id
        assert posted[0].amount == Decimal("85.50")
        assert posted[0].source_type == "sale"
        assert posted[0].source_id == sale.id
        db.session.commit()

        duplicate = record_sale_financial_movements(
            sale,
            {"efectivo": Decimal("10.00"), "mercado_pago": Decimal("85.50")},
        )
        assert duplicate == []
        assert FinancialAccountMovement.query.filter_by(
            company_id=company.id,
            idempotency_key="sale:98765:mercado_pago",
        ).count() == 1
