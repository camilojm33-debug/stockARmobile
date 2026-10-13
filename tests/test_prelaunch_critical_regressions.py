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
