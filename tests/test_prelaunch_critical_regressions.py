import json
from datetime import datetime, timedelta
from decimal import Decimal

from app import Company, Plan, User, db, load_user
from services.backup_service import BackupService
from services.plan_service import PlanService
from stockarmobile.permissions import employee_endpoint_permission


def test_plan_bootstrap_preserves_existing_admin_configuration(app):
    with app.app_context():
        existing = Plan(
            code="business",
            name="Plan configurado por Superadmin",
            price=Decimal("43123.00"),
            currency="ARS",
            duration_days=45,
            max_users=17,
            max_products=4321,
            max_clients=7654,
            features_json="inventario,ventas,crm",
            state="paused",
            active=False,
        )
        db.session.add(existing)
        db.session.commit()

        PlanService.ensure_defaults(db.session)
        db.session.refresh(existing)

        assert existing.name == "Plan configurado por Superadmin"
        assert float(existing.price) == 43123.0
        assert existing.duration_days == 45
        assert existing.max_users == 17
        assert existing.max_products == 4321
        assert existing.max_clients == 7654
        assert existing.features_json == "inventario,ventas,crm"
        assert existing.state == "paused"
        assert existing.active is False

        existing_codes = {row.code for row in Plan.query.all()}
        assert {"trial", "entrepreneur", "business", "premium"} <= existing_codes


def test_deactivated_user_is_not_restored_from_an_existing_login_session(app):
    with app.app_context():
        user = User(
            username="revoked_employee",
            email="revoked_employee@example.test",
            password_hash="unused-test-hash",
            role="user",
            active=False,
        )
        db.session.add(user)
        db.session.commit()
        user_id = user.id

        assert load_user(str(user_id)) is None


def test_ai_and_invoice_endpoints_require_explicit_employee_permissions():
    assert employee_endpoint_permission("whatsapp_agent.ai_whatsapp_attention", "GET") == "ai_access"
    assert employee_endpoint_permission("whatsapp_agent.ai_whatsapp_attention_action", "POST") == "ai_access"
    assert employee_endpoint_permission("whatsapp_agent.ai_whatsapp_attention_message", "POST") == "ai_access"
    assert employee_endpoint_permission("resolve_invoice_code_direct", "POST") == "ai_access"
    assert employee_endpoint_permission("dashboard.resolve_invoice_line", "POST") == "ai_access"
    assert employee_endpoint_permission("dashboard.process_invoice", "POST") == "ai_access"


def test_backup_restore_does_not_restore_paid_ai_entitlements():
    current = {
        "timezone": "America/Argentina/Cordoba",
        "ai_agent": {"plan_code": "inicio", "status": "VENCIDA", "enabled": False},
    }
    backup = {
        "timezone": "UTC",
        "ai_agent": {"plan_code": "pro", "status": "ACTIVA", "enabled": True},
        "layout": "compact",
    }

    restored = json.loads(
        BackupService._preserve_ai_entitlement_preferences(
            current_raw=json.dumps(current),
            restored_raw=json.dumps(backup),
        )
    )

    assert restored["timezone"] == "UTC"
    assert restored["layout"] == "compact"
    assert restored["ai_agent"] == current["ai_agent"]


def test_backup_cannot_invent_ai_entitlement_if_tenant_has_none():
    backup = {
        "timezone": "UTC",
        "ai_agent": {"plan_code": "pro", "status": "ACTIVA", "enabled": True},
    }

    restored = json.loads(
        BackupService._preserve_ai_entitlement_preferences(
            current_raw=json.dumps({"timezone": "America/Argentina/Cordoba"}),
            restored_raw=json.dumps(backup),
        )
    )

    assert restored == {"timezone": "UTC"}



def test_approved_ai_payment_credits_access_once_and_late_preapproval_does_not_extend_it(app):
    from services.ai_agent.subscription_service import AISubscriptionService

    with app.app_context():
        base = datetime(2026, 10, 1, 12, 0, 0)
        company = Company(
            name="AI renewal QA",
            active=True,
            preferences_json=json.dumps({
                "ai_agent": {
                    "plan_code": "pro",
                    "status": "PENDIENTE",
                    "origin": "MERCADO_PAGO",
                    "mercadopago_preapproval_id": "preapproval-qa",
                    "ends_at": (base + timedelta(days=19)).isoformat(),
                }
            }),
        )
        db.session.add(company)
        db.session.commit()

        AISubscriptionService.record_mercadopago_payment(
            company,
            payment_id="payment-qa-1",
            payment_status="approved",
            amount=1000,
            currency="ARS",
            paid_at=base,
            preapproval_id="preapproval-qa",
            external_reference=f"ai_subscription:true|company_id:{company.id}",
        )
        after_first_payment = json.loads(company.preferences_json)["ai_agent"]
        expected_end = base + timedelta(days=30)
        assert datetime.fromisoformat(after_first_payment["ends_at"]) == expected_end

        # A retried notification must not credit another month.
        AISubscriptionService.record_mercadopago_payment(
            company,
            payment_id="payment-qa-1",
            payment_status="approved",
            amount=1000,
            currency="ARS",
            paid_at=base,
            preapproval_id="preapproval-qa",
            external_reference=f"ai_subscription:true|company_id:{company.id}",
        )
        after_retry = json.loads(company.preferences_json)["ai_agent"]
        assert datetime.fromisoformat(after_retry["ends_at"]) == expected_end
        assert after_retry["mercadopago_paid_payment_ids"].count("payment-qa-1") == 1

        # An authorization event arriving afterward must not replace the
        # already credited period with an inconsistent next_payment_date.
        AISubscriptionService.sync_from_mercadopago(preapproval={
            "id": "preapproval-qa",
            "external_reference": f"ai_subscription:true|company_id:{company.id}",
            "status": "authorized",
            "next_payment_date": (base + timedelta(days=60)).isoformat(),
        })
        after_preapproval = json.loads(company.preferences_json)["ai_agent"]
        assert datetime.fromisoformat(after_preapproval["ends_at"]) == expected_end


def test_ai_subscription_cancel_preserves_paid_access_until_expiry(app, monkeypatch):
    from services.ai_agent.subscription_service import AISubscriptionService

    class FakeMercadoPago:
        def cancel_preapproval(self, preapproval_id):
            assert preapproval_id == "preapproval-cancel"
            return {"id": preapproval_id, "status": "cancelled"}

    with app.app_context():
        paid_until = datetime.utcnow().replace(microsecond=0) + timedelta(days=12)
        company = Company(
            name="AI cancel QA",
            active=True,
            preferences_json=json.dumps({
                "ai_agent": {
                    "plan_code": "pro",
                    "status": "ACTIVA",
                    "origin": "MERCADO_PAGO",
                    "mercadopago_preapproval_id": "preapproval-cancel",
                    "mercadopago_status": "authorized",
                    "ends_at": paid_until.isoformat(),
                }
            }),
        )
        db.session.add(company)
        db.session.commit()

        monkeypatch.setattr(
            AISubscriptionService,
            "_get_mp_preapproval",
            classmethod(lambda cls, row: {"id": "preapproval-cancel", "status": "authorized"}),
        )
        monkeypatch.setattr(AISubscriptionService, "_mp_service", staticmethod(lambda: FakeMercadoPago()))

        AISubscriptionService.cancel(company, admin_user_id=None, reason="QA")
        persisted = json.loads(company.preferences_json)["ai_agent"]
        assert persisted["mercadopago_status"] == "cancelled"
        assert persisted["status"] == "ACTIVA"
        assert datetime.fromisoformat(persisted["ends_at"]) == paid_until



def test_purchase_edit_reverses_original_recorded_value_before_recalculating_average():
    from purchases import _recalculate_purchase_edit_values

    # Existing inventory: 5 units at $20 and 5 units at $25 -> WAC $22.50.
    # Editing the second purchase from $25 to $20 must leave WAC at $20.
    stock, average_cost = _recalculate_purchase_edit_values(
        current_stock=10,
        current_cost=22.5,
        old_quantity=5,
        old_value=125,
        new_quantity=5,
        new_value=100,
        product_name="Producto QA",
    )
    assert stock == 10
    assert average_cost == 20


def test_purchase_edit_refuses_to_create_negative_inventory_value():
    import pytest
    from purchases import _recalculate_purchase_edit_values

    with pytest.raises(ValueError, match="valor actual del stock"):
        _recalculate_purchase_edit_values(
            current_stock=2,
            current_cost=10,
            old_quantity=2,
            old_value=50,
            new_quantity=2,
            new_value=40,
            product_name="Producto QA",
        )


def test_invoice_confirmation_rejects_header_total_not_supported_by_lines():
    import pytest
    from services.invoice_purchase_service import InvoicePurchaseError, InvoicePurchaseService

    with pytest.raises(InvoicePurchaseError, match="subtotal de la factura no coincide"):
        InvoicePurchaseService._validate_line_subtotal(
            [{"line_number": 1, "quantity": "2", "unit_cost": "40", "line_total": "80"}],
            Decimal("120"),
        )


def test_invoice_confirmation_accepts_consistent_line_subtotal():
    from services.invoice_purchase_service import InvoicePurchaseService

    subtotal = InvoicePurchaseService._validate_line_subtotal(
        [
            {"line_number": 1, "quantity": "2", "unit_cost": "40", "line_total": "80"},
            {"line_number": 2, "quantity": "1", "unit_cost": "20", "line_total": "20"},
        ],
        Decimal("100"),
    )
    assert subtotal == Decimal("100")


def test_cash_expense_records_egress_and_reduces_expected_drawer_balance(app):
    from app import CashSession, Expense
    from cash import _session_summary
    from expenses import _record_cash_expense_movement

    with app.app_context():
        company = Company(name="Caja QA", active=True)
        db.session.add(company)
        db.session.flush()
        user = User(
            username="cash_qa_admin",
            email="cash_qa_admin@example.test",
            password_hash="unused-test-hash",
            role="admin",
            active=True,
            company_id=company.id,
        )
        db.session.add(user)
        db.session.flush()
        cash_session = CashSession(
            user_id=user.id,
            company_id=company.id,
            opened_by_user_id=user.id,
            opening_amount=Decimal("100.00"),
            status="abierta",
        )
        db.session.add(cash_session)
        db.session.flush()
        expense = Expense(
            category="Otros",
            description="Gasto efectivo QA",
            amount=Decimal("25.00"),
            payment_method="EFECTIVO",
            user_id=user.id,
            company_id=company.id,
        )
        db.session.add(expense)
        db.session.flush()

        movement = _record_cash_expense_movement(user=user, expense=expense)
        assert movement is not None
        db.session.commit()
        db.session.refresh(cash_session)
        assert _session_summary(cash_session)["expected"] == Decimal("75.00")


def test_non_cash_expense_does_not_change_the_open_drawer(app):
    from app import CashSession, Expense
    from cash import _session_summary
    from expenses import _record_cash_expense_movement

    with app.app_context():
        company = Company(name="Caja sin efectivo QA", active=True)
        db.session.add(company)
        db.session.flush()
        user = User(
            username="cash_non_cash_admin",
            email="cash_non_cash_admin@example.test",
            password_hash="unused-test-hash",
            role="admin",
            active=True,
            company_id=company.id,
        )
        db.session.add(user)
        db.session.flush()
        cash_session = CashSession(
            user_id=user.id,
            company_id=company.id,
            opened_by_user_id=user.id,
            opening_amount=Decimal("100.00"),
            status="abierta",
        )
        db.session.add(cash_session)
        db.session.flush()
        expense = Expense(
            category="Otros",
            description="Gasto tarjeta QA",
            amount=Decimal("25.00"),
            payment_method="TARJETA",
            user_id=user.id,
            company_id=company.id,
        )
        db.session.add(expense)
        db.session.flush()

        assert _record_cash_expense_movement(user=user, expense=expense) is None
        db.session.commit()
        db.session.refresh(cash_session)
        assert _session_summary(cash_session)["expected"] == Decimal("100.00")


def test_sales_report_excludes_cancelled_sales(app):
    from datetime import datetime, timedelta
    from flask_login import login_user
    from app import Sale
    from reports import _rows_for

    with app.app_context():
        company = Company(name="Report QA", active=True)
        db.session.add(company)
        db.session.flush()
        user = User(
            username="report_qa_admin",
            email="report_qa_admin@example.test",
            password_hash="unused-test-hash",
            role="admin",
            active=True,
            company_id=company.id,
        )
        db.session.add(user)
        db.session.flush()
        now = datetime.utcnow()
        db.session.add_all([
            Sale(company_id=company.id, customer="Anulada", total_amount=Decimal("100.00"), status="anulada", date=now),
            Sale(company_id=company.id, customer="Confirmada", total_amount=Decimal("200.00"), status="confirmada", date=now),
        ])
        db.session.commit()
        with app.test_request_context("/reportes/?desde=2026-10-13&hasta=2026-10-13"):
            login_user(user)
            rows, _ = _rows_for("ventas")
        assert len(rows) == 2
        assert rows[1][1] == "Confirmada"
        assert rows[1][2] == Decimal("200.00")



def test_empty_database_bootstrap_creates_only_historical_baseline_tables(app):
    import importlib.util
    from pathlib import Path
    from sqlalchemy import inspect

    migration_path = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "versions"
        / "20260712_02_hardening_numeric_money_columns.py"
    )
    spec = importlib.util.spec_from_file_location("stockarmobile_baseline_migration_20260712_02", migration_path)
    migration = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(migration)

    with app.app_context():
        db.session.remove()
        db.drop_all()
        bind = db.engine
        migration._ensure_baseline_schema(bind)
        inspector = inspect(bind)
        tables = set(inspector.get_table_names())

        assert {"companies", "users", "plans", "payments", "subscriptions", "products"} <= tables
        # These tables are created by later revisions. They must not be
        # pre-created from today's model metadata during the baseline revision.
        assert "ai_campaigns" not in tables
        assert "crm_opportunities" not in tables
        assert "crm_activities" not in tables
        assert "saas_lead_suppressions" not in tables



def test_recurring_payment_does_not_add_month_to_provider_synced_boundary(monkeypatch):
    from datetime import datetime, timedelta
    from types import SimpleNamespace
    from services.subscription_service import SubscriptionService

    paid_at = datetime(2026, 10, 13, 12, 0, 0)
    provider_boundary = paid_at + timedelta(days=30)
    metadata = {
        "is_manual": False,
        "checkout_method": "automatic",
        "mercadopago_preapproval_id": "preapproval-order-1",
        "mercadopago_next_payment_date": provider_boundary.isoformat(),
    }
    subscription = SimpleNamespace(
        id=17,
        company_id=9,
        status="active",
        plan=SimpleNamespace(duration_days=30),
        metadata_json=json.dumps(metadata),
        next_billing_date=provider_boundary,
        ends_at=provider_boundary,
        start_date=paid_at - timedelta(days=30),
        starts_at=paid_at - timedelta(days=30),
        renewal_enabled=True,
        auto_renew=True,
        cancel_at_period_end=False,
        last_payment_date=None,
    )
    monkeypatch.setattr(
        SubscriptionService,
        "_target_subscription_for_command",
        staticmethod(lambda company_id, subscription_id: subscription),
    )
    monkeypatch.setattr(
        SubscriptionService,
        "active_subscription_for_company",
        staticmethod(lambda company_id: subscription),
    )

    SubscriptionService._handle_renew(
        object(),
        company=SimpleNamespace(id=9),
        command=SubscriptionService.RenewSubscriptionCommand(
            company_id=9,
            subscription_id=17,
            payment_status="approved",
            paid_at=paid_at,
            renewal_enabled=True,
            origin="subscription_authorized_payment",
        ),
    )

    assert subscription.next_billing_date == provider_boundary
    assert subscription.ends_at == provider_boundary
    assert subscription.last_payment_date == paid_at
    assert subscription.renewal_enabled is True


def test_recurring_payment_without_provider_boundary_does_not_stack_on_placeholder_date(monkeypatch):
    from datetime import datetime, timedelta
    from types import SimpleNamespace
    from services.subscription_service import SubscriptionService

    paid_at = datetime(2026, 10, 13, 12, 0, 0)
    placeholder = paid_at + timedelta(days=60)
    metadata = {
        "is_manual": False,
        "checkout_method": "automatic",
        "mercadopago_preapproval_id": "preapproval-order-2",
    }
    subscription = SimpleNamespace(
        id=18,
        company_id=9,
        status="active",
        plan=SimpleNamespace(duration_days=30),
        metadata_json=json.dumps(metadata),
        next_billing_date=placeholder,
        ends_at=placeholder,
        start_date=paid_at - timedelta(days=10),
        starts_at=paid_at - timedelta(days=10),
        renewal_enabled=True,
        auto_renew=True,
        cancel_at_period_end=False,
        last_payment_date=None,
    )
    monkeypatch.setattr(
        SubscriptionService,
        "_target_subscription_for_command",
        staticmethod(lambda company_id, subscription_id: subscription),
    )
    monkeypatch.setattr(
        SubscriptionService,
        "active_subscription_for_company",
        staticmethod(lambda company_id: subscription),
    )

    SubscriptionService._handle_renew(
        object(),
        company=SimpleNamespace(id=9),
        command=SubscriptionService.RenewSubscriptionCommand(
            company_id=9,
            subscription_id=18,
            payment_status="approved",
            paid_at=paid_at,
            renewal_enabled=True,
            origin="subscription_authorized_payment",
        ),
    )

    assert subscription.next_billing_date == paid_at + timedelta(days=30)
    assert subscription.ends_at == paid_at + timedelta(days=30)
