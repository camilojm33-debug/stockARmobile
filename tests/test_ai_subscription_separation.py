import json
from copy import deepcopy

import pytest
from flask_login import login_user

import app as stock_app
from app import Company, Payment, Plan, Subscription, User, db
from services.ai_agent.config_service import update_ai_preferences
from services.ai_agent.subscription_service import AISubscriptionService
from services.payment_flow import (
    FLOW_AI_ORDER,
    FLOW_AI_SUBSCRIPTION,
    FLOW_OTHER,
    FLOW_POS,
    FLOW_STANDARD,
    ai_subscription_payment_filter,
    payment_flow,
    standard_subscription_payment_filter,
    subscription_revenue_payment_filter,
)
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


def _tenant_without_standard_subscription():
    company = Company(name="Empresa Solo IA", active=True, contact_email="solo-ia@test.local")
    db.session.add(company)
    db.session.flush()
    user = User(username="solo_ia", email="solo-ia@test.local", password_hash="x", role="admin", active=True, company_id=company.id)
    plan = Plan(code="standard", name="Standard", price=1000, currency="ARS", duration_days=30, active=True)
    db.session.add_all([user, plan])
    db.session.commit()
    return company, user, plan


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


def test_subscription_portal_uses_separate_ai_payment_method_forms(subscription_app):
    _, user, _, _ = _tenant_with_standard_subscription()
    client = subscription_app.test_client()
    _login(client, user)

    response = client.get("/admin/portal")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'action="/admin/subscription/ai-agent/checkout"' in html
    assert 'type="hidden" name="payment_method" value="automatic"' in html
    assert 'type="hidden" name="payment_method" value="qr"' in html
    assert 'type="submit" name="payment_method"' not in html
    assert 'data-mp-external-checkout="true"' in html


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


def test_standard_active_ai_active_different_plan_does_not_cancel_or_create(subscription_app, monkeypatch):
    company, user, _, subscription = _tenant_with_standard_subscription()
    update_ai_preferences(company, ai_updates={"plan_code": "inicio", "status": "ACTIVA", "origin": "MERCADO_PAGO", "mercadopago_preapproval_id": "ai-pre-old"})
    db.session.commit()
    ai_before = deepcopy(AISubscriptionService.get_status(company))
    before = _standard_snapshot(subscription)
    calls = _mock_preapproval(monkeypatch, created_id="ai-pre-new-plan", init_point="https://mp.test/ai-new-plan")
    cancelled = []

    def cancel_preapproval(self, preapproval_id):
        cancelled.append(preapproval_id)
        return {"id": preapproval_id, "status": "canceled"}

    monkeypatch.setattr("services.mercadopago_service.MercadoPagoService.cancel_preapproval", cancel_preapproval)
    monkeypatch.setattr(AISubscriptionService, "_get_mp_preapproval", lambda company: {"id": "ai-pre", "status": "pending"})
    client = subscription_app.test_client()
    _login(client, user)

    response = client.post("/admin/subscription/ai-agent/checkout", data={"plan_code": "vendedor", "payment_method": "automatic"})

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/agentes-ia/planes")
    assert cancelled == []
    assert calls == []
    assert AISubscriptionService.get_status(company) == ai_before
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


def test_ai_pending_abandoned_checkout_remains_pending(subscription_app, monkeypatch):
    company, user, _, subscription = _tenant_with_standard_subscription()
    calls = _mock_preapproval(monkeypatch, created_id="ai-pre-abandoned", init_point="https://mp.test/abandoned")
    client = subscription_app.test_client()
    _login(client, user)

    response = client.post("/admin/subscription/ai-agent/checkout", data={"plan_code": "inicio", "payment_method": "automatic"})

    assert response.status_code == 302
    assert response.headers["Location"] == "https://mp.test/abandoned"
    assert calls[0][0] == "create"
    status = AISubscriptionService.get_status(company)
    assert status["status"] == "PENDIENTE"
    assert status["mercadopago_preapproval_id"] == "ai-pre-abandoned"
    assert _standard_snapshot(subscription)["status"] == SubscriptionService.STATE_ACTIVE


def test_double_click_ai_checkout_reuses_pending_preapproval(subscription_app, monkeypatch):
    company, user, _, subscription = _tenant_with_standard_subscription()
    calls = _mock_preapproval(monkeypatch, created_id="ai-pre-double", init_point="https://mp.test/double")
    client = subscription_app.test_client()
    _login(client, user)

    first = client.post("/admin/subscription/ai-agent/checkout", data={"plan_code": "inicio", "payment_method": "automatic"})
    second = client.post("/admin/subscription/ai-agent/checkout", data={"plan_code": "inicio", "payment_method": "automatic"})

    assert first.headers["Location"] == "https://mp.test/double"
    assert second.headers["Location"] == "https://mp.test/ai-pre-double"
    assert [kind for kind, _ in calls] == ["create", "get"]
    assert AISubscriptionService.get_status(company)["status"] == "PENDIENTE"
    assert _standard_snapshot(subscription)["status"] == SubscriptionService.STATE_ACTIVE


def test_standard_active_ai_pending_different_plan_does_not_cancel_or_create(subscription_app, monkeypatch):
    company, user, _, subscription = _tenant_with_standard_subscription()
    update_ai_preferences(company, ai_updates={"plan_code": "inicio", "status": "PENDIENTE", "origin": "MERCADO_PAGO", "mercadopago_preapproval_id": "ai-pre-pending"})
    db.session.commit()
    ai_before = deepcopy(AISubscriptionService.get_status(company))
    before = _standard_snapshot(subscription)
    calls = _mock_preapproval(monkeypatch, created_id="ai-pre-pending-change", init_point="https://mp.test/ai-pending-change")
    cancelled = []

    def cancel_preapproval(self, preapproval_id):
        cancelled.append(preapproval_id)
        return {"id": preapproval_id, "status": "canceled"}

    monkeypatch.setattr("services.mercadopago_service.MercadoPagoService.cancel_preapproval", cancel_preapproval)
    monkeypatch.setattr(AISubscriptionService, "_get_mp_preapproval", lambda company: {"id": "ai-pre", "status": "pending"})
    client = subscription_app.test_client()
    _login(client, user)

    response = client.post("/admin/subscription/ai-agent/checkout", data={"plan_code": "negocio", "payment_method": "automatic"})

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/agentes-ia/planes")
    assert cancelled == []
    assert calls == []
    assert AISubscriptionService.get_status(company) == ai_before
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


def test_ai_management_does_not_modify_standard_subscription(subscription_app, monkeypatch):
    company, _, _, subscription = _tenant_with_standard_subscription()
    update_ai_preferences(company, ai_updates={"plan_code": "inicio", "status": "ACTIVA", "origin": "MERCADO_PAGO", "mercadopago_preapproval_id": "ai-pre"})
    db.session.commit()
    before = _standard_snapshot(subscription)
    monkeypatch.setattr(AISubscriptionService, "_get_mp_preapproval", lambda company: {"id": "ai-pre", "status": "authorized"})
    monkeypatch.setattr("services.mercadopago_service.MercadoPagoService.cancel_preapproval", lambda self, preapproval_id: {"id": preapproval_id, "status": "cancelled"})

    AISubscriptionService.cancel(company, admin_user_id=None, reason="tenant requested")

    assert AISubscriptionService.get_status(company)["status"] == "CANCELADA"
    assert _standard_snapshot(subscription) == before


def test_explicit_tenant_ai_cancel_does_not_modify_standard_subscription(subscription_app, monkeypatch):
    company, user, _, subscription = _tenant_with_standard_subscription()
    update_ai_preferences(company, ai_updates={"plan_code": "inicio", "status": "PENDIENTE", "origin": "MERCADO_PAGO", "mercadopago_preapproval_id": "ai-pre"})
    db.session.commit()
    before = _standard_snapshot(subscription)
    cancelled = []

    def cancel_preapproval(self, preapproval_id):
        cancelled.append(preapproval_id)
        return {"id": preapproval_id, "status": "cancelled"}

    monkeypatch.setattr("services.mercadopago_service.MercadoPagoService.cancel_preapproval", cancel_preapproval)
    client = subscription_app.test_client()
    _login(client, user)

    response = client.post("/admin/subscription/ai-agent/cancel", data={"confirm_ai_cancel": "1"})

    assert response.status_code == 302
    assert cancelled == ["ai-pre"]
    assert AISubscriptionService.get_status(company)["status"] == "CANCELADA"
    assert _standard_snapshot(subscription) == before


def test_explicit_tenant_ai_cancel_requires_confirmation(subscription_app, monkeypatch):
    company, user, _, subscription = _tenant_with_standard_subscription()
    update_ai_preferences(company, ai_updates={"plan_code": "inicio", "status": "ACTIVA", "origin": "MERCADO_PAGO", "mercadopago_preapproval_id": "ai-pre"})
    db.session.commit()
    ai_before = deepcopy(AISubscriptionService.get_status(company))
    before = _standard_snapshot(subscription)
    cancelled = []
    monkeypatch.setattr("services.mercadopago_service.MercadoPagoService.cancel_preapproval", lambda self, preapproval_id: cancelled.append(preapproval_id))
    client = subscription_app.test_client()
    _login(client, user)

    response = client.post("/admin/subscription/ai-agent/cancel", data={})

    assert response.status_code == 302
    assert cancelled == []
    assert AISubscriptionService.get_status(company) == ai_before
    assert _standard_snapshot(subscription) == before


def test_standard_cancel_does_not_modify_ai_subscription(subscription_app):
    company, user, _, subscription = _tenant_with_standard_subscription()
    update_ai_preferences(company, ai_updates={"plan_code": "inicio", "status": "ACTIVA", "origin": "MERCADO_PAGO", "mercadopago_preapproval_id": "ai-pre"})
    db.session.commit()
    ai_before = deepcopy(AISubscriptionService.get_status(company))
    client = subscription_app.test_client()
    _login(client, user)

    response = client.post("/admin/subscription/cancel")

    assert response.status_code == 302
    db.session.refresh(subscription)
    assert subscription.cancel_at_period_end is True
    assert AISubscriptionService.get_status(company) == ai_before


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


def test_standard_auto_subscription_checkout_works_when_ai_is_active(subscription_app, monkeypatch):
    company, user, _, subscription = _tenant_with_standard_subscription()
    update_ai_preferences(company, ai_updates={"plan_code": "inicio", "status": "ACTIVA", "origin": "MERCADO_PAGO", "mercadopago_preapproval_id": "ai-pre"})
    db.session.commit()
    ai_before = deepcopy(AISubscriptionService.get_status(company))
    calls = []

    def create_standard_preapproval(*, db_session, company, subscription, plan, payer_email, notification_url, back_url):
        calls.append({
            "company_id": company.id,
            "subscription_id": subscription.id,
            "plan_id": plan.id,
            "payer_email": payer_email,
            "notification_url": notification_url,
            "back_url": back_url,
        })
        return {"id": "standard-pre", "init_point": "https://mp.test/standard", "status": "pending"}

    monkeypatch.setattr("services.mercadopago_subscription_service.MercadoPagoSubscriptionService.create", create_standard_preapproval)
    client = subscription_app.test_client()
    _login(client, user)

    response = client.post("/admin/subscription/mercadopago/create", data={"plan_id": subscription.plan_id})

    assert response.status_code == 302
    assert response.headers["Location"] == "https://mp.test/standard"
    assert calls == [{
        "company_id": company.id,
        "subscription_id": subscription.id,
        "plan_id": subscription.plan_id,
        "payer_email": user.email,
        "notification_url": calls[0]["notification_url"],
        "back_url": calls[0]["back_url"],
    }]
    assert AISubscriptionService.get_status(company) == ai_before


def test_standard_missing_ai_active_can_start_standard_checkout(subscription_app, monkeypatch):
    company, user, plan = _tenant_without_standard_subscription()
    update_ai_preferences(company, ai_updates={"plan_code": "inicio", "status": "ACTIVA", "origin": "MERCADO_PAGO", "mercadopago_preapproval_id": "ai-pre"})
    db.session.commit()
    ai_before = deepcopy(AISubscriptionService.get_status(company))

    def create_standard_preapproval(*, db_session, company, subscription, plan, payer_email, notification_url, back_url):
        return {"id": "standard-pre", "init_point": "https://mp.test/standard", "status": "pending"}

    monkeypatch.setattr("services.mercadopago_subscription_service.MercadoPagoSubscriptionService.create", create_standard_preapproval)
    client = subscription_app.test_client()
    _login(client, user)

    response = client.post("/admin/subscription/mercadopago/create", data={"plan_id": plan.id})

    assert response.status_code == 302
    assert response.headers["Location"] == "https://mp.test/standard"
    assert Subscription.query.filter_by(company_id=company.id).count() == 1
    assert AISubscriptionService.get_status(company) == ai_before


def test_standard_auto_subscription_checkout_returns_json_redirect_for_ajax(subscription_app, monkeypatch):
    company, user, _, subscription = _tenant_with_standard_subscription()
    update_ai_preferences(company, ai_updates={"plan_code": "inicio", "status": "ACTIVA", "origin": "MERCADO_PAGO", "mercadopago_preapproval_id": "ai-pre"})
    db.session.commit()

    def create_standard_preapproval(*, db_session, company, subscription, plan, payer_email, notification_url, back_url):
        return {"id": "standard-pre", "init_point": "https://mp.test/standard", "status": "pending"}

    monkeypatch.setattr("services.mercadopago_subscription_service.MercadoPagoSubscriptionService.create", create_standard_preapproval)
    client = subscription_app.test_client()
    _login(client, user)

    response = client.post(
        "/admin/subscription/mercadopago/create",
        data={"plan_id": subscription.plan_id},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )

    assert response.status_code == 200
    assert response.get_json() == {"success": True, "redirect_url": "https://mp.test/standard"}


def test_ai_auto_subscription_checkout_returns_json_redirect_for_ajax(subscription_app, monkeypatch):
    company, user, _, subscription = _tenant_with_standard_subscription()
    update_ai_preferences(company, ai_updates={"plan_code": "inicio", "status": "PENDIENTE", "origin": "MERCADO_PAGO", "mercadopago_preapproval_id": "ai-pre"})
    db.session.commit()
    _mock_preapproval(monkeypatch)
    client = subscription_app.test_client()
    _login(client, user)

    response = client.post(
        "/admin/subscription/ai-agent/checkout",
        data={"plan_code": "inicio", "payment_method": "automatic"},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )

    assert response.status_code == 200
    assert response.get_json() == {"success": True, "redirect_url": "https://mp.test/ai-pre"}
    assert _standard_snapshot(subscription)["status"] == SubscriptionService.STATE_ACTIVE


def test_ai_webhook_cancelled_updates_only_ai_subscription(subscription_app, monkeypatch):
    company, _, _, subscription = _tenant_with_standard_subscription()
    before = _standard_snapshot(subscription)
    update_ai_preferences(company, ai_updates={"plan_code": "inicio", "status": "ACTIVA", "origin": "MERCADO_PAGO", "mercadopago_preapproval_id": "ai-pre"})
    db.session.commit()
    service = WebhookService()
    monkeypatch.setattr(service.mp_service, "validate_webhook_signature", lambda **kwargs: True)
    monkeypatch.setattr(service.mp_service, "get_preapproval", lambda data_id: {"id": "ai-pre", "status": "cancelled", "external_reference": f"ai_subscription:true|company_id:{company.id}|plan_code:inicio|nonce:abc"})

    result = service.process(db_session=db.session, headers={"x-request-id": "rq-ai-cancel", "x-signature": "ts=1,v1=abc"}, payload={"id": "evt-ai-cancel", "type": "subscription_preapproval", "data": {"id": "ai-pre"}})

    assert result["status"] == "processed_ai_subscription"
    assert AISubscriptionService.get_status(company)["status"] == "CANCELADA"
    assert _standard_snapshot(subscription) == before


def test_failed_ai_recurring_payment_does_not_cancel_ai_or_standard(subscription_app, monkeypatch):
    company, _, _, subscription = _tenant_with_standard_subscription()
    update_ai_preferences(company, ai_updates={"plan_code": "inicio", "status": "ACTIVA", "origin": "MERCADO_PAGO", "mercadopago_preapproval_id": "ai-pre"})
    db.session.commit()
    ai_before = deepcopy(AISubscriptionService.get_status(company))
    before = _standard_snapshot(subscription)
    service = WebhookService()
    monkeypatch.setattr(service.mp_service, "validate_webhook_signature", lambda **kwargs: True)
    monkeypatch.setattr(service.mp_service, "get_authorized_payment", lambda data_id: {"preapproval_id": "ai-pre", "external_reference": f"ai_subscription:true|company_id:{company.id}|plan_code:inicio|nonce:abc", "payment": {"id": "pay-ai-failed", "status": "rejected"}})

    result = service.process(db_session=db.session, headers={"x-request-id": "rq-ai-pay-failed", "x-signature": "ts=1,v1=abc"}, payload={"id": "evt-ai-pay-failed", "type": "subscription_authorized_payment", "data": {"id": "auth-pay-failed"}})

    assert result["status"] == "processed_ai_subscription_payment"
    assert result["payment_status"] == "rejected"
    status = AISubscriptionService.get_status(company)
    assert status["status"] == ai_before["status"] == "ACTIVA"
    assert status["plan_code"] == ai_before["plan_code"] == "inicio"
    assert status["last_payment_id"] == "pay-ai-failed"
    assert status["last_payment_status"] == "rejected"
    assert _standard_snapshot(subscription) == before


def test_ai_recurring_payment_authorized_renews_only_ai(subscription_app, monkeypatch):
    company, _, _, subscription = _tenant_with_standard_subscription()
    update_ai_preferences(company, ai_updates={"plan_code": "inicio", "status": "ACTIVA", "origin": "MERCADO_PAGO", "mercadopago_preapproval_id": "ai-pre", "ends_at": "2026-09-20T00:00:00"})
    db.session.commit()
    before = _standard_snapshot(subscription)
    service = WebhookService()
    monkeypatch.setattr(service.mp_service, "validate_webhook_signature", lambda **kwargs: True)
    monkeypatch.setattr(service.mp_service, "get_authorized_payment", lambda data_id: {"preapproval_id": "ai-pre", "external_reference": f"ai_subscription:true|company_id:{company.id}|plan_code:inicio|nonce:abc", "transaction_amount": 11385, "currency_id": "ARS", "debit_date": "2026-09-21T00:00:00Z", "payment": {"id": "pay-ai-ok", "status": "approved"}})
    monkeypatch.setattr(service.mp_service, "get_preapproval", lambda preapproval_id: {"id": "ai-pre", "status": "authorized", "next_payment_date": "2026-10-20T00:00:00Z", "external_reference": f"ai_subscription:true|company_id:{company.id}|plan_code:inicio|nonce:abc"})

    result = service.process(db_session=db.session, headers={"x-request-id": "rq-ai-pay-ok", "x-signature": "ts=1,v1=abc"}, payload={"id": "evt-ai-pay-ok", "type": "subscription_authorized_payment", "data": {"id": "auth-pay-ok"}})

    status = AISubscriptionService.get_status(company)
    assert result["status"] == "processed_ai_subscription_payment"
    assert status["status"] == "ACTIVA"
    assert status["ends_at"].startswith("2026-10-20")
    assert status["last_payment_id"] == "pay-ai-ok"
    assert status["last_payment_status"] == "approved"
    payment = Payment.query.filter_by(payment_id="pay-ai-ok").one()
    assert payment.provider == "mercadopago_ai_subscription"
    assert payment.subscription_id is None
    assert payment.company_id == company.id
    assert _standard_snapshot(subscription) == before


def test_repeated_ai_authorized_payment_does_not_duplicate_payment_or_renewal(subscription_app, monkeypatch):
    company, _, _, subscription = _tenant_with_standard_subscription()
    update_ai_preferences(company, ai_updates={"plan_code": "inicio", "status": "ACTIVA", "origin": "MERCADO_PAGO", "mercadopago_preapproval_id": "ai-pre", "ends_at": "2026-09-20T00:00:00"})
    db.session.commit()
    before = _standard_snapshot(subscription)
    get_preapproval_calls = []
    service = WebhookService()
    monkeypatch.setattr(service.mp_service, "validate_webhook_signature", lambda **kwargs: True)
    monkeypatch.setattr(service.mp_service, "get_authorized_payment", lambda data_id: {"preapproval_id": "ai-pre", "external_reference": f"ai_subscription:true|company_id:{company.id}|plan_code:inicio|nonce:abc", "transaction_amount": 11385, "currency_id": "ARS", "payment": {"id": "pay-ai-repeat", "status": "approved"}})

    def get_preapproval(preapproval_id):
        get_preapproval_calls.append(preapproval_id)
        return {"id": "ai-pre", "status": "authorized", "next_payment_date": "2026-10-20T00:00:00Z", "external_reference": f"ai_subscription:true|company_id:{company.id}|plan_code:inicio|nonce:abc"}

    monkeypatch.setattr(service.mp_service, "get_preapproval", get_preapproval)

    first = service.process(db_session=db.session, headers={"x-request-id": "rq-ai-repeat-1", "x-signature": "ts=1,v1=abc"}, payload={"id": "evt-ai-repeat-1", "type": "subscription_authorized_payment", "data": {"id": "auth-pay-repeat-1"}})
    db.session.commit()
    second = service.process(db_session=db.session, headers={"x-request-id": "rq-ai-repeat-2", "x-signature": "ts=1,v1=abc"}, payload={"id": "evt-ai-repeat-2", "type": "subscription_authorized_payment", "data": {"id": "auth-pay-repeat-2"}})

    assert first["status"] == "processed_ai_subscription_payment"
    assert second["status"] == "processed_ai_subscription_payment"
    assert Payment.query.filter_by(payment_id="pay-ai-repeat").count() == 1
    assert get_preapproval_calls == ["ai-pre"]
    assert AISubscriptionService.get_status(company)["ends_at"].startswith("2026-10-20")
    assert _standard_snapshot(subscription) == before


def test_ai_payments_do_not_appear_as_standard_payment_rows(subscription_app):
    company, user, _, subscription = _tenant_with_standard_subscription()
    db.session.add(Payment(payment_id="pay-ai-hidden", external_reference=f"ai_subscription:true|company_id:{company.id}|plan_code:inicio|nonce:abc", company_id=company.id, subscription_id=None, amount=11385, currency="ARS", status="approved", payment_method="mercadopago_ai_subscription", reference="ai-pre", provider="mercadopago_ai_subscription"))
    db.session.commit()
    client = subscription_app.test_client()
    _login(client, user)

    response = client.get("/admin/portal")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Pago IA" in html
    assert "pay-ai-hidden" in html
    standard_history = html.split('id="historial-pagos"', 1)[1]
    assert "pay-ai-hidden" not in standard_history
    assert _standard_snapshot(subscription)["status"] == SubscriptionService.STATE_ACTIVE


def test_payment_flow_classifies_standard_ai_pos_ai_order_and_other(subscription_app):
    company, _, _, subscription = _tenant_with_standard_subscription()
    rows = [
        Payment(payment_id="standard", company_id=company.id, subscription_id=subscription.id, provider="mercadopago_subscription"),
        Payment(payment_id="ai", company_id=company.id, subscription_id=None, provider="mercadopago_ai_subscription", external_reference=f"ai_subscription:true|company_id:{company.id}|plan_code:inicio|nonce:abc"),
        Payment(payment_id="pos", company_id=company.id, subscription_id=None, provider="mercadopago_pos", external_reference=f"flow:pos_sale|company_id:{company.id}"),
        Payment(payment_id="ai-order", company_id=company.id, subscription_id=None, provider="mercadopago_ai_order", external_reference=f"flow:ai_order|company_id:{company.id}"),
        Payment(payment_id="other", company_id=company.id, subscription_id=None, provider="manual"),
    ]
    db.session.add_all(rows)
    db.session.flush()

    assert [payment_flow(row) for row in rows] == [FLOW_STANDARD, FLOW_AI_SUBSCRIPTION, FLOW_POS, FLOW_AI_ORDER, FLOW_OTHER]


def test_subscription_revenue_splits_standard_ai_and_total(subscription_app):
    company, _, _, subscription = _tenant_with_standard_subscription()
    db.session.add_all([
        Payment(payment_id="standard-ok", company_id=company.id, subscription_id=subscription.id, amount=1000, currency="ARS", status="approved", provider="mercadopago_subscription"),
        Payment(payment_id="ai-ok", company_id=company.id, subscription_id=None, amount=200, currency="ARS", status="approved", provider="mercadopago_ai_subscription", external_reference=f"ai_subscription:true|company_id:{company.id}|plan_code:inicio|nonce:abc"),
        Payment(payment_id="pos-ok", company_id=company.id, subscription_id=None, amount=500, currency="ARS", status="approved", provider="mercadopago_pos", external_reference=f"flow:pos_sale|company_id:{company.id}"),
    ])
    db.session.commit()

    standard_total = db.session.query(db.func.coalesce(db.func.sum(Payment.amount), 0)).filter(standard_subscription_payment_filter(Payment), Payment.status == "approved").scalar()
    ai_total = db.session.query(db.func.coalesce(db.func.sum(Payment.amount), 0)).filter(ai_subscription_payment_filter(Payment), Payment.status == "approved").scalar()
    revenue_total = db.session.query(db.func.coalesce(db.func.sum(Payment.amount), 0)).filter(subscription_revenue_payment_filter(Payment), Payment.status == "approved").scalar()

    assert float(standard_total or 0) == 1000.0
    assert float(ai_total or 0) == 200.0
    assert float(revenue_total or 0) == 1200.0


def test_standard_payment_pdf_rejects_ai_subscription_payment(subscription_app):
    company, user, _, _ = _tenant_with_standard_subscription()
    payment = Payment(payment_id="ai-pdf", company_id=company.id, subscription_id=None, amount=200, currency="ARS", status="approved", provider="mercadopago_ai_subscription", external_reference=f"ai_subscription:true|company_id:{company.id}|plan_code:inicio|nonce:abc")
    db.session.add(payment)
    db.session.commit()
    client = subscription_app.test_client()
    _login(client, user)

    response = client.get(f"/admin/subscription/payments/{payment.id}/pdf")

    assert response.status_code == 404


def test_superadmin_company_detail_separates_standard_and_ai_payments(subscription_app):
    company, _, _, subscription = _tenant_with_standard_subscription()
    superadmin = User(username="super_detail", email="super_detail@test.local", password_hash="x", role="superadmin", active=True)
    db.session.add_all([
        superadmin,
        Payment(payment_id="standard-detail", company_id=company.id, subscription_id=subscription.id, amount=1000, currency="ARS", status="approved", provider="mercadopago_subscription"),
        Payment(payment_id="ai-detail", company_id=company.id, subscription_id=None, amount=200, currency="ARS", status="approved", provider="mercadopago_ai_subscription", external_reference=f"ai_subscription:true|company_id:{company.id}|plan_code:inicio|nonce:abc"),
    ])
    db.session.commit()

    with subscription_app.test_request_context(f"/superadmin/companies/{company.id}"):
        login_user(superadmin)
        from saas import company_detail
        response = company_detail(company.id)
        html = response if isinstance(response, str) else response.get_data(as_text=True)

    assert "Pagos aprobados total" in html
    assert "Standard $1000.00" in html
    assert "IA $200.00" in html
    assert "Standard · standard-detail" in html
    assert "Suscripción IA · ai-detail" in html


def test_superadmin_billing_and_payments_show_payment_types(subscription_app):
    company, _, _, subscription = _tenant_with_standard_subscription()
    superadmin = User(username="super_payments", email="super_payments@test.local", password_hash="x", role="superadmin", active=True)
    db.session.add_all([
        superadmin,
        Payment(payment_id="standard-billing", company_id=company.id, subscription_id=subscription.id, amount=1000, currency="ARS", status="approved", provider="mercadopago_subscription"),
        Payment(payment_id="ai-billing", company_id=company.id, subscription_id=None, amount=200, currency="ARS", status="approved", provider="mercadopago_ai_subscription", external_reference=f"ai_subscription:true|company_id:{company.id}|plan_code:inicio|nonce:abc"),
    ])
    db.session.commit()

    with subscription_app.test_request_context("/superadmin/billing"):
        login_user(superadmin)
        from saas import billing
        response = billing()
        billing_html = response if isinstance(response, str) else response.get_data(as_text=True)
    with subscription_app.test_request_context("/superadmin/payments"):
        login_user(superadmin)
        from saas import payments_panel
        response = payments_panel()
        payments_html = response if isinstance(response, str) else response.get_data(as_text=True)

    assert "Standard $1000.00" in billing_html
    assert "IA $200.00" in billing_html
    assert "Suscripción IA" in billing_html
    assert "Suscripción IA" in payments_html
    assert "Standard" in payments_html


def test_superadmin_notifications_separate_standard_and_ai_payments(subscription_app):
    company, _, _, subscription = _tenant_with_standard_subscription()
    db.session.add_all([
        Payment(payment_id="standard-pending", company_id=company.id, subscription_id=subscription.id, amount=1000, currency="ARS", status="pending", provider="mercadopago_subscription"),
        Payment(payment_id="ai-approved", company_id=company.id, subscription_id=None, amount=200, currency="ARS", status="approved", provider="mercadopago_ai_subscription", external_reference=f"ai_subscription:true|company_id:{company.id}|plan_code:inicio|nonce:abc"),
    ])
    db.session.commit()

    from services.notification_service import _build_superadmin_notifications
    items = _build_superadmin_notifications()
    payments_item = next(item for item in items if item["title"] == "Pagos")

    assert "1 pendientes (1 Standard · 0 IA)" in payments_item["body"]
    assert "1 aprobados hoy (0 Standard · 1 IA)" in payments_item["body"]


def test_superadmin_manual_activation_updates_only_ai(subscription_app):
    company, _, _, subscription = _tenant_with_standard_subscription()
    admin = User(username="super_ai", email="super_ai@test.local", password_hash="x", role="superadmin", active=True)
    db.session.add(admin)
    db.session.commit()
    before = _standard_snapshot(subscription)
    client = subscription_app.test_client()
    _login(client, admin)

    response = client.post(f"/superadmin/ai-subscriptions/{company.id}/action", data={"action": "assign_plan", "plan_code": "pro"})

    assert response.status_code == 302
    status = AISubscriptionService.get_status(company)
    assert status["plan_code"] == "pro"
    assert status["status"] == "ACTIVA"
    assert status["origin"] == "MANUAL"
    assert _standard_snapshot(subscription) == before
