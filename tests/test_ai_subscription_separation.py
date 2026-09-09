import json
from copy import deepcopy

import pytest

import app as stock_app
from app import Company, Plan, Subscription, User, db
from services.ai_agent.config_service import update_ai_preferences
from services.ai_agent.subscription_service import AISubscriptionService
from services.subscription_service import SubscriptionService
from services.webhook_service import WebhookService


@pytest.fixture
def subscription_app():
    stock_app.app.config["TESTING"] = True
    stock_app.app.config["WTF_CSRF_ENABLED"] = False
    stock_app.app.config["SERVER_NAME"] = "localhost"
    stock_app.app.config["APP_URL"] = "http://test.local"

    with stock_app.app.app_context():
        db.drop_all()
        db.create_all()
        yield stock_app.app
        db.session.remove()
        db.drop_all()


def _tenant_with_standard_subscription():
    company = Company(name="Empresa Separada", active=True, contact_email="admin@test.local")
    db.session.add(company)
    db.session.flush()
    user = User(username="admin_sep", email="admin@test.local", password_hash="x", role="admin", active=True, company_id=company.id)
    plan = Plan(code="standard", name="Standard", price=1000, currency="ARS", duration_days=30, active=True)
    db.session.add_all([user, plan])
    db.session.flush()
    subscription = Subscription(company_id=company.id, plan_id=plan.id, status=SubscriptionService.STATE_ACTIVE, renewal_enabled=True, auto_renew=True)
    db.session.add(subscription)
    db.session.commit()
    return company, user, plan, subscription


def _login(client, user):
    with client.session_transaction() as session:
        session["_user_id"] = str(user.id)
        session["_fresh"] = True


def _mock_preapproval(monkeypatch, *, created_id="ai-pre-new", init_point="https://mp.test/ai-new"):
    calls = []

    def create_preapproval(self, **kwargs):
        calls.append(("create", kwargs))
        return {"id": created_id, "init_point": init_point, "status": "pending"}

    def get_preapproval(self, preapproval_id):
        calls.append(("get", preapproval_id))
        return {"id": preapproval_id, "init_point": f"https://mp.test/{preapproval_id}", "status": "pending"}

    monkeypatch.setattr("services.mercadopago_service.MercadoPagoService.create_preapproval", create_preapproval)
    monkeypatch.setattr("services.mercadopago_service.MercadoPagoService.get_preapproval", get_preapproval)
    return calls


def _standard_snapshot(subscription):
    db.session.refresh(subscription)
    return {
        "id": subscription.id,
        "plan_id": subscription.plan_id,
        "status": subscription.status,
        "renewal_enabled": subscription.renewal_enabled,
        "auto_renew": subscription.auto_renew,
        "metadata_json": subscription.metadata_json,
    }


def test_standard_active_ai_missing_can_start_ai_checkout(subscription_app, monkeypatch):
    company, user, _, subscription = _tenant_with_standard_subscription()
    before = _standard_snapshot(subscription)
    calls = _mock_preapproval(monkeypatch)
    client = subscription_app.test_client()
    _login(client, user)

    response = client.post("/admin/subscription/ai-agent/checkout", data={"plan_code": "inicio", "payment_method": "automatic"})

    assert response.status_code == 302
    assert response.headers["Location"] == "https://mp.test/ai-new"
    assert calls[0][0] == "create"
    assert calls[0][1]["amount"] == AISubscriptionService.plan_amount_ars("inicio")
    assert "ai_subscription:true" in calls[0][1]["external_reference"]
    assert AISubscriptionService.get_status(company)["status"] == "PENDIENTE"
    assert _standard_snapshot(subscription) == before


def test_ai_plans_page_posts_directly_to_ai_checkout(subscription_app):
    _, user, _, _ = _tenant_with_standard_subscription()
    client = subscription_app.test_client()
    _login(client, user)

    response = client.get("/agentes-ia/planes")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'action="/admin/subscription/ai-agent/checkout"' in html
    assert 'name="payment_method" value="automatic"' in html
    assert 'name="payment_method" value="qr"' in html
    assert '>Contratar<' not in html


def test_standard_active_ai_active_blocks_same_ai_plan_only(subscription_app, monkeypatch):
    company, user, _, subscription = _tenant_with_standard_subscription()
    update_ai_preferences(company, ai_updates={"plan_code": "inicio", "status": "ACTIVA", "origin": "MERCADO_PAGO", "mercadopago_preapproval_id": "ai-pre"})
    db.session.commit()
    before = _standard_snapshot(subscription)
    calls = _mock_preapproval(monkeypatch)
    client = subscription_app.test_client()
    _login(client, user)

    response = client.post("/admin/subscription/ai-agent/checkout", data={"plan_code": "inicio", "payment_method": "automatic"})

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/agentes-ia/planes")
    assert calls == []
    assert AISubscriptionService.get_status(company)["status"] == "ACTIVA"
    assert _standard_snapshot(subscription) == before


def test_standard_active_ai_pending_can_continue_same_ai_checkout(subscription_app, monkeypatch):
    company, user, _, subscription = _tenant_with_standard_subscription()
    update_ai_preferences(company, ai_updates={"plan_code": "inicio", "status": "PENDIENTE", "origin": "MERCADO_PAGO", "mercadopago_preapproval_id": "ai-pre"})
    db.session.commit()
    before = _standard_snapshot(subscription)
    calls = _mock_preapproval(monkeypatch)
    client = subscription_app.test_client()
    _login(client, user)

    response = client.post("/admin/subscription/ai-agent/checkout", data={"plan_code": "inicio", "payment_method": "automatic"})

    assert response.status_code == 302
    assert response.headers["Location"] == "https://mp.test/ai-pre"
    assert calls == [("get", "ai-pre")]
    assert AISubscriptionService.get_status(company)["status"] == "PENDIENTE"
    assert _standard_snapshot(subscription) == before


def test_standard_active_ai_cancelled_can_start_ai_again(subscription_app, monkeypatch):
    company, user, _, subscription = _tenant_with_standard_subscription()
    update_ai_preferences(company, ai_updates={"plan_code": "inicio", "status": "CANCELADA", "origin": "MERCADO_PAGO", "mercadopago_preapproval_id": "ai-old"})
    db.session.commit()
    before = _standard_snapshot(subscription)
    calls = _mock_preapproval(monkeypatch, created_id="ai-pre-renew", init_point="https://mp.test/ai-renew")
    client = subscription_app.test_client()
    _login(client, user)

    response = client.post("/admin/subscription/ai-agent/checkout", data={"plan_code": "inicio", "payment_method": "automatic"})

    assert response.status_code == 302
    assert response.headers["Location"] == "https://mp.test/ai-renew"
    assert calls[0][0] == "create"
    status = AISubscriptionService.get_status(company)
    assert status["status"] == "PENDIENTE"
    assert status["mercadopago_preapproval_id"] == "ai-pre-renew"
    assert _standard_snapshot(subscription) == before


def test_ai_management_does_not_modify_standard_subscription(subscription_app):
    company, _, _, subscription = _tenant_with_standard_subscription()
    update_ai_preferences(company, ai_updates={"plan_code": "inicio", "status": "ACTIVA", "origin": "MERCADO_PAGO", "mercadopago_preapproval_id": "ai-pre"})
    db.session.commit()
    before = _standard_snapshot(subscription)

    AISubscriptionService.cancel(company, admin_user_id=None, reason="tenant requested")

    assert AISubscriptionService.get_status(company)["status"] == "CANCELADA"
    assert _standard_snapshot(subscription) == before


def test_ai_checkout_action_does_not_modify_standard_subscription(subscription_app, monkeypatch):
    company, user, _, subscription = _tenant_with_standard_subscription()
    before = _standard_snapshot(subscription)
    _mock_preapproval(monkeypatch)
    client = subscription_app.test_client()
    _login(client, user)

    response = client.post("/admin/subscription/ai-agent/checkout", data={"plan_code": "vendedor", "payment_method": "qr"})

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/admin/portal?checkout=ai_created#suscripcion-ia")
    assert AISubscriptionService.get_status(company)["status"] == "PENDIENTE"
    assert _standard_snapshot(subscription) == before


def test_ai_webhook_updates_only_ai_subscription(subscription_app, monkeypatch):
    company, _, _, subscription = _tenant_with_standard_subscription()
    before = _standard_snapshot(subscription)
    update_ai_preferences(company, ai_updates={"plan_code": "inicio", "status": "PENDIENTE", "origin": "MERCADO_PAGO", "mercadopago_preapproval_id": "ai-pre"})
    db.session.commit()
    service = WebhookService()
    monkeypatch.setattr(service.mp_service, "validate_webhook_signature", lambda **kwargs: True)
    monkeypatch.setattr(service.mp_service, "get_preapproval", lambda data_id: {"id": "ai-pre", "status": "authorized", "next_payment_date": "2026-10-09T12:00:00Z", "external_reference": f"ai_subscription:true|company_id:{company.id}|plan_code:inicio|nonce:abc"})

    result = service.process(db_session=db.session, headers={"x-request-id": "rq-ai", "x-signature": "ts=1,v1=abc"}, payload={"id": "evt-ai", "type": "subscription_preapproval", "data": {"id": "ai-pre"}})

    assert result["status"] == "processed_ai_subscription"
    assert AISubscriptionService.get_status(company)["status"] == "ACTIVA"
    assert _standard_snapshot(subscription) == before


def test_standard_webhook_updates_only_standard_subscription(subscription_app, monkeypatch):
    company, _, _, subscription = _tenant_with_standard_subscription()
    update_ai_preferences(company, ai_updates={"plan_code": "inicio", "status": "PENDIENTE", "origin": "MERCADO_PAGO", "mercadopago_preapproval_id": "ai-pre"})
    SubscriptionService._set_metadata(subscription, {"mercadopago_preapproval_id": "standard-pre"})
    db.session.commit()
    ai_before = deepcopy(AISubscriptionService.get_status(company))
    service = WebhookService()
    monkeypatch.setattr(service.mp_service, "validate_webhook_signature", lambda **kwargs: True)
    monkeypatch.setattr(service.mp_service, "get_preapproval", lambda data_id: {"id": "standard-pre", "status": "authorized", "next_payment_date": "2026-10-09T12:00:00Z", "external_reference": f"stockarmobile|flow:subscription_auto|company_id:{company.id}|subscription_id:{subscription.id}|nonce:def"})

    result = service.process(db_session=db.session, headers={"x-request-id": "rq-standard", "x-signature": "ts=1,v1=abc"}, payload={"id": "evt-standard", "type": "subscription_preapproval", "data": {"id": "standard-pre"}})

    db.session.refresh(subscription)
    assert result["status"] == "processed"
    assert result["subscription_id"] == subscription.id
    assert subscription.status == SubscriptionService.STATE_ACTIVE
    assert subscription.auto_renew is True
    assert AISubscriptionService.get_status(company) == ai_before
