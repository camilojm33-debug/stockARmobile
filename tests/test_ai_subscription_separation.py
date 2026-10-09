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


def test_ai_recurring_checkout_reuses_attempt_after_ambiguous_mp_timeout(subscription_app, monkeypatch):
    company, user, _, subscription = _tenant_with_standard_subscription()
    standard_before = _standard_snapshot(subscription)
    seen_references = []
    client = subscription_app.test_client()
    _login(client, user)

    def timeout_after_mp_may_have_created(self, **kwargs):
        seen_references.append(kwargs["external_reference"])
        raise RuntimeError("simulated network timeout after request")

    monkeypatch.setattr(
        "services.mercadopago_service.MercadoPagoService.create_preapproval",
        timeout_after_mp_may_have_created,
    )
    first = client.post(
        "/admin/subscription/ai-agent/checkout",
        data={"plan_code": "inicio", "payment_method": "automatic"},
    )
    assert first.status_code in {302, 409, 500}
    first_status = AISubscriptionService.get_status(company)
    first_attempt = first_status["mercadopago_create_attempt_reference"]
    assert first_attempt
    assert first_status["mercadopago_create_attempt_plan_code"] == "inicio"

    def successful_idempotent_retry(self, **kwargs):
        seen_references.append(kwargs["external_reference"])
        return {
            "id": "ai-pre-recovered",
            "status": "pending",
            "init_point": "https://www.mercadopago.com.ar/subscriptions/checkout?preapproval_id=ai-pre-recovered",
        }

    monkeypatch.setattr(
        "services.mercadopago_service.MercadoPagoService.create_preapproval",
        successful_idempotent_retry,
    )
    second = client.post(
        "/admin/subscription/ai-agent/checkout",
        data={"plan_code": "inicio", "payment_method": "automatic"},
    )

    assert second.status_code == 302
    assert seen_references == [first_attempt, first_attempt]
    current_ai = AISubscriptionService.get_status(company)
    assert current_ai["mercadopago_preapproval_id"] == "ai-pre-recovered"
    assert current_ai["mercadopago_create_attempt_reference"] is None
    assert current_ai["mercadopago_create_attempt_plan_code"] is None
    assert _standard_snapshot(subscription) == standard_before


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


def test_standard_plan_selection_redirects_directly_to_mercadopago(subscription_app, monkeypatch):
    _, user, _, _ = _tenant_with_standard_subscription()

    def fake_checkout(*args, **kwargs):
        return {"preference": {"id": "pref-direct", "init_point": "https://mp.test/standard-direct"}}

    monkeypatch.setattr("company_billing.BillingService.create_checkout_for_plan", fake_checkout)
    client = subscription_app.test_client()
    _login(client, user)

    response = client.post("/admin/checkout", data={"direct_checkout": "1"})

    assert response.status_code == 302
    assert response.headers["Location"] == "https://mp.test/standard-direct"


def test_standard_recurring_checkout_reuses_in_process_preapproval(monkeypatch):
    from services.mercadopago_subscription_service import MercadoPagoSubscriptionService

    class DummyPlan:
        id = 1
        name = "Standard"
        price = 1000
        currency = "ARS"

    class DummySubscription:
        metadata_json = json.dumps({"mercadopago_preapproval_id": "preapproval-in-process"})

    class DummyCompany:
        id = 1

    calls = []
    monkeypatch.setattr(
        "services.mercadopago_subscription_service.MercadoPagoService.get_preapproval",
        lambda self, preapproval_id: {
            "id": preapproval_id,
            "status": "in_process",
            "init_point": "https://www.mercadopago.com.ar/subscriptions/checkout?preapproval_id=preapproval-in-process",
        },
    )
    monkeypatch.setattr(
        "services.mercadopago_subscription_service.SubscriptionService._metadata_dict",
        lambda subscription: json.loads(subscription.metadata_json),
    )
    monkeypatch.setattr(
        "services.mercadopago_subscription_service.SubscriptionService._set_metadata",
        lambda subscription, updates: calls.append(updates),
    )

    result = MercadoPagoSubscriptionService.create(
        db_session=None,
        company=DummyCompany(),
        subscription=DummySubscription(),
        plan=DummyPlan(),
        payer_email="admin@example.com",
        notification_url="https://stockarmobile.com/webhook",
        back_url="https://stockarmobile.com/portal",
    )

    assert result["status"] == "in_process"
    assert result["init_point"].startswith("https://www.mercadopago.com.ar/")
    assert calls == [{
        "mercadopago_status": "in_process",
        "mercadopago_external_reference": None,
        "mercadopago_creation_pending": False,
        "checkout_method": "automatic",
        "checkout_cancelled": False,
        "payment_method": "mercadopago_subscription",
    }]


def test_subscription_portal_uses_separate_ai_payment_method_forms(subscription_app):
    _, user, _, _ = _tenant_with_standard_subscription()
    db.session.add(Plan(code="entrepreneur", name="Emprendedor", price=12000, currency="ARS", duration_days=30, active=True))
    db.session.commit()
    client = subscription_app.test_client()
    _login(client, user)

    response = client.get("/admin/portal")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'action="/admin/subscription/ai-agent/checkout"' in html
    assert 'type="hidden" name="payment_method" value="automatic"' in html
    assert 'action="/admin/subscription/mercadopago/create"' in html
    assert '>Suscripción mensual automática</button>' in html
    assert '>Pagar este plan con QR</button>' in html
    assert 'action="/admin/checkout"' in html
    assert 'name="plan_id"' in html
    assert 'type="hidden" name="payment_method" value="qr"' in html
    assert 'type="submit" name="payment_method"' not in html
    assert 'data-mp-external-checkout="true"' in html


def test_standard_plan_change_offers_qr_and_monthly_destinations(subscription_app):
    _, user, _, _ = _tenant_with_standard_subscription()
    target_plan = Plan(code="standard_upgrade", name="Standard Upgrade", price=2000, currency="ARS", duration_days=30, active=True)
    db.session.add(target_plan)
    db.session.commit()
    client = subscription_app.test_client()
    _login(client, user)

    response = client.get(f"/admin/portal?selected_plan_id={target_plan.id}")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'action="/admin/subscription/change"' in html
    assert 'name="payment_method" value="qr"' in html
    assert 'action="/admin/subscription/mercadopago/create"' in html
    # Este botón crea una suscripción mensual recurrente; direct_checkout pertenece
    # al selector estándar de planes, no a este endpoint de autorización automática.
    assert 'data-mp-external-checkout="true"' in html
    assert "Pagar con QR" in html
    assert "Suscripción mensual en Mercado Pago" in html


def test_pending_standard_checkout_can_be_replaced_with_cheaper_plan(subscription_app, monkeypatch):
    company, user, _, active_subscription = _tenant_with_standard_subscription()
    premium = Plan(code="premium", name="Premium", price=54999, currency="ARS", duration_days=30, active=True)
    cheaper = Plan(code="entrepreneur", name="Emprendedor", price=12999, currency="ARS", duration_days=30, active=True)
    db.session.add_all([premium, cheaper])
    db.session.flush()
    command = SubscriptionService.ChangePlanCommand(
        company_id=company.id,
        plan_id=premium.id,
        actor_user_id=user.id,
        actor_role=user.role,
        origin="portal_confirm",
        idempotency_key="pending-premium-checkout",
    )
    pending_result = SubscriptionService.run_command(db.session, command)
    old_pending = db.session.get(Subscription, pending_result.subscription_id)
    from services.billing_notification_service import NotificationService

    NotificationService.record_event(
        db.session,
        company_id=company.id,
        subscription_id=old_pending.id,
        event="checkout_preference_created",
        detail="Preference old-premium para Premium",
        source="mercadopago",
        status="pending",
        event_id="old-premium-preference",
        payload={"id": "old-premium-preference", "init_point": "https://mp.test/old-premium"},
        user_id=user.id,
    )
    db.session.commit()
    old_pending_id = old_pending.id
    cheaper_id = cheaper.id
    created = {}

    def create_checkout(self, *, db_session, company, plan, user, subscription):
        created["subscription"] = subscription
        return {
            "subscription": subscription,
            "preference": {"id": "cheaper-preference", "init_point": "https://mp.test/cheaper"},
        }

    monkeypatch.setattr("company_billing.BillingService.create_checkout_for_plan", create_checkout)
    client = subscription_app.test_client()
    _login(client, user)

    response = client.post("/admin/subscription/change", data={"plan_id": cheaper_id})

    assert response.status_code == 302
    assert "checkout_preference_id=cheaper-preference" in response.headers["Location"]
    assert created["subscription"].plan_id == cheaper_id
    db.session.refresh(old_pending)
    db.session.refresh(active_subscription)
    assert old_pending.status == SubscriptionService.STATE_CANCELLED
    assert SubscriptionService._metadata_dict(old_pending)["checkout_superseded"] is True
    assert active_subscription.status == SubscriptionService.STATE_ACTIVE
    assert Subscription.query.filter_by(company_id=company.id, plan_id=cheaper_id, status=SubscriptionService.STATE_PENDING_PAYMENT).count() == 1


def test_plan_change_replacement_is_blocked_when_old_payment_is_in_process(subscription_app, monkeypatch):
    company, user, _, _ = _tenant_with_standard_subscription()
    premium = Plan(code="premium_in_process", name="Premium", price=54999, currency="ARS", duration_days=30, active=True)
    cheaper = Plan(code="entrepreneur_in_process", name="Emprendedor", price=12999, currency="ARS", duration_days=30, active=True)
    db.session.add_all([premium, cheaper])
    db.session.flush()
    result = SubscriptionService.run_command(
        db.session,
        SubscriptionService.ChangePlanCommand(
            company_id=company.id,
            plan_id=premium.id,
            actor_user_id=user.id,
            actor_role=user.role,
            origin="portal_confirm",
            idempotency_key="pending-premium-in-process",
        ),
    )
    pending = db.session.get(Subscription, result.subscription_id)
    from services.billing_notification_service import NotificationService

    NotificationService.record_event(
        db.session,
        company_id=company.id,
        subscription_id=pending.id,
        event="checkout_preference_created",
        detail="Preference old-premium-in-process",
        source="mercadopago",
        status="pending",
        event_id="old-premium-in-process-preference",
        payload={"id": "old-premium-in-process-preference", "init_point": "https://mp.test/old-premium"},
        user_id=user.id,
    )
    payment = Payment(
        payment_id="12345678901",
        preference_id="old-premium-in-process-preference",
        external_reference="old-premium-in-process",
        company_id=company.id,
        subscription_id=pending.id,
        user_id=user.id,
        amount=54999,
        currency="ARS",
        status="in_process",
        provider="mercadopago",
    )
    db.session.add(payment)
    db.session.commit()
    pending_id = pending.id
    cheaper_id = cheaper.id
    calls = []
    monkeypatch.setattr(
        "company_billing.BillingService.create_checkout_for_plan",
        lambda *args, **kwargs: calls.append("created"),
    )
    client = subscription_app.test_client()
    _login(client, user)

    response = client.post("/admin/subscription/change", data={"plan_id": cheaper_id})

    assert response.status_code == 302
    assert f"checkout_subscription_id={pending_id}" in response.headers["Location"]
    assert calls == []
    db.session.refresh(pending)
    assert pending.status == SubscriptionService.STATE_PENDING_PAYMENT
    assert Subscription.query.filter_by(company_id=company.id, plan_id=cheaper_id).count() == 0


def test_pending_standard_monthly_checkout_can_be_replaced_with_cheaper_plan(subscription_app, monkeypatch):
    company, user, _, active_subscription = _tenant_with_standard_subscription()
    premium = Plan(code="premium_monthly", name="Premium", price=54999, currency="ARS", duration_days=30, active=True)
    cheaper = Plan(code="entrepreneur_monthly", name="Emprendedor", price=12999, currency="ARS", duration_days=30, active=True)
    db.session.add_all([premium, cheaper])
    db.session.flush()
    result = SubscriptionService.run_command(
        db.session,
        SubscriptionService.ChangePlanCommand(
            company_id=company.id,
            plan_id=premium.id,
            actor_user_id=user.id,
            actor_role=user.role,
            origin="portal_confirm",
            idempotency_key="pending-premium-monthly",
        ),
    )
    old_pending = db.session.get(Subscription, result.subscription_id)
    SubscriptionService._set_metadata(
        old_pending,
        {"mercadopago_preapproval_id": "old-premium-preapproval", "mercadopago_status": "pending"},
    )
    db.session.commit()
    cheaper_id = cheaper.id
    calls = []

    def get_preapproval(self, preapproval_id):
        calls.append(("get", preapproval_id))
        return {"id": preapproval_id, "status": "pending"}

    def cancel_preapproval(self, preapproval_id):
        calls.append(("cancel", preapproval_id))
        return {"id": preapproval_id, "status": "cancelled"}

    def create_preapproval(*, db_session, company, subscription, plan, payer_email, notification_url, back_url):
        calls.append(("create", subscription.plan_id))
        return {"id": "cheaper-preapproval", "init_point": "https://mp.test/cheaper-monthly", "status": "pending"}

    monkeypatch.setattr("services.mercadopago_service.MercadoPagoService.get_preapproval", get_preapproval)
    monkeypatch.setattr("services.mercadopago_service.MercadoPagoService.cancel_preapproval", cancel_preapproval)
    monkeypatch.setattr("services.mercadopago_subscription_service.MercadoPagoSubscriptionService.create", create_preapproval)
    client = subscription_app.test_client()
    _login(client, user)

    response = client.post("/admin/subscription/mercadopago/create", data={"plan_id": cheaper_id})

    assert response.status_code == 302
    assert response.headers["Location"] == "https://mp.test/cheaper-monthly"
    assert calls == [
        ("get", "old-premium-preapproval"),
        ("cancel", "old-premium-preapproval"),
        ("create", cheaper_id),
    ]
    db.session.refresh(old_pending)
    db.session.refresh(active_subscription)
    assert old_pending.status == SubscriptionService.STATE_CANCELLED
    assert active_subscription.status == SubscriptionService.STATE_ACTIVE
    assert Subscription.query.filter_by(company_id=company.id, plan_id=cheaper_id, status=SubscriptionService.STATE_PENDING_PAYMENT).count() == 1


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

    plans_page = client.get("/agentes-ia/planes")
    plans_html = plans_page.get_data(as_text=True)
    assert plans_page.status_code == 200
    assert 'var subscriptionStatus="ACTIVA"' in plans_html
    assert "Ya hay un plan IA activo" in plans_html

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


def test_pending_ai_checkout_locks_other_method_and_keeps_standard_independent(subscription_app, monkeypatch):
    company, user, _, subscription = _tenant_with_standard_subscription()
    before = _standard_snapshot(subscription)
    update_ai_preferences(
        company,
        ai_updates={
            "plan_code": "vendedor",
            "status": "PENDIENTE",
            "origin": "MERCADO_PAGO",
            "mercadopago_preapproval_id": "ai-pre-locked",
            "mercadopago_status": "pending",
            "checkout_method": "qr",
        },
    )
    db.session.commit()
    calls = []

    def get_preapproval(self, preapproval_id):
        calls.append(("get", preapproval_id))
        return {"id": preapproval_id, "status": "pending", "init_point": "https://mp.test/legacy-ai-locked"}

    def cancel_preapproval(self, preapproval_id):
        calls.append(("cancel", preapproval_id))
        return {"id": preapproval_id, "status": "cancelled"}

    def create_qr_preference(self, **kwargs):
        calls.append(("create_qr", kwargs))
        return {"id": "ai-qr-pref-locked", "init_point": "https://mp.test/ai-qr-locked"}

    monkeypatch.setattr("services.mercadopago_service.MercadoPagoService.get_preapproval", get_preapproval)
    monkeypatch.setattr("services.mercadopago_service.MercadoPagoService.cancel_preapproval", cancel_preapproval)
    monkeypatch.setattr("services.mercadopago_service.MercadoPagoService.create_ai_subscription_qr_preference", create_qr_preference)
    client = subscription_app.test_client()
    _login(client, user)

    continue_qr = client.post(
        "/admin/subscription/ai-agent/checkout",
        data={"plan_code": "vendedor", "payment_method": "qr"},
    )
    qr_payment = Payment.query.filter_by(company_id=company.id, provider="mercadopago_ai_qr").first()
    alternate_method = client.post(
        "/admin/subscription/ai-agent/checkout",
        data={"plan_code": "vendedor", "payment_method": "automatic"},
    )
    portal = client.get("/admin/portal")
    html = portal.get_data(as_text=True)

    assert continue_qr.status_code == 302
    assert f"ai_qr_payment_id={qr_payment.id}" in continue_qr.headers["Location"]
    assert alternate_method.status_code == 302
    assert f"ai_qr_payment_id={qr_payment.id}" in alternate_method.headers["Location"]
    assert [call[0] for call in calls] == ["get", "cancel", "create_qr"]
    assert AISubscriptionService.get_status(company)["status"] == "CANCELADA"
    assert qr_payment.status == "pending"
    assert "Pago QR pendiente" in html or "Pago único de 30 días" in html
    assert 'data-ai-checkout-method=""' in html
    assert 'data-standard-checkout-method=""' in html
    assert _standard_snapshot(subscription) == before

    ai_plans = client.get("/agentes-ia/planes")
    ai_plans_html = ai_plans.get_data(as_text=True)
    assert ai_plans.status_code == 200
    assert 'var pendingQrPlanCode="vendedor"' in ai_plans_html
    assert "Continuar con QR" in ai_plans_html
    assert "Pago QR pendiente" in ai_plans_html or "Pago QR pendiente:" in ai_plans_html


@pytest.mark.parametrize(
    ("checkout_method", "attempt_endpoint"),
    [
        ("qr", "/admin/subscription/mercadopago/create"),
        ("automatic", "/admin/checkout"),
    ],
)
def test_standard_pending_checkout_locks_only_the_other_method(subscription_app, monkeypatch, checkout_method, attempt_endpoint):
    company, user, plan, subscription = _tenant_with_standard_subscription()
    metadata = {"checkout_method": checkout_method}
    if checkout_method == "automatic":
        metadata.update({"mercadopago_preapproval_id": "standard-pre-locked", "mercadopago_status": "pending"})
    SubscriptionService._set_metadata(subscription, metadata)
    db.session.commit()
    if checkout_method == "qr":
        from services.billing_notification_service import NotificationService

        NotificationService.record_event(
            db.session,
            company_id=company.id,
            subscription_id=subscription.id,
            event="checkout_preference_created",
            detail="Pending standard QR",
            source="mercadopago",
            status="pending",
            event_id="standard-qr-locked",
            payload={"id": "standard-qr-locked", "init_point": "https://mp.test/standard-qr"},
            user_id=user.id,
        )
        db.session.commit()

    created = []
    monkeypatch.setattr(
        "company_billing.BillingService.create_checkout_for_plan",
        lambda *args, **kwargs: created.append("qr"),
    )
    monkeypatch.setattr(
        "services.mercadopago_subscription_service.MercadoPagoSubscriptionService.create",
        lambda *args, **kwargs: created.append("automatic"),
    )
    client = subscription_app.test_client()
    _login(client, user)

    if attempt_endpoint.endswith("/checkout"):
        response = client.post(attempt_endpoint, data={"plan_id": plan.id})
    else:
        response = client.post(attempt_endpoint, data={"plan_id": plan.id})
    portal = client.get("/admin/portal")
    html = portal.get_data(as_text=True)

    assert response.status_code == 302
    assert created == []
    assert f'data-standard-checkout-method="{checkout_method}"' in html
    assert 'data-ai-checkout-status=""' in html
    assert subscription.status == SubscriptionService.STATE_ACTIVE


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
    monkeypatch.setattr(AISubscriptionService, "_get_mp_preapproval", lambda company: {"id": "ai-pre", "status": "pending"})
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
    captured = {}

    def create_qr_preference(self, **kwargs):
        captured.update(kwargs)
        return {"id": "ai-qr-pref-01", "init_point": "https://mp.test/ai-qr-01"}

    monkeypatch.setattr(
        "services.mercadopago_service.MercadoPagoService.create_ai_subscription_qr_preference",
        create_qr_preference,
    )
    client = subscription_app.test_client()
    _login(client, user)

    response = client.post(
        "/admin/subscription/ai-agent/checkout",
        data={"plan_code": "vendedor", "payment_method": "qr"},
    )

    payment = Payment.query.filter_by(company_id=company.id, provider="mercadopago_ai_qr").one()
    assert response.status_code == 302
    assert f"ai_qr_payment_id={payment.id}" in response.headers["Location"]
    assert captured["plan_code"] == "vendedor"
    assert captured["amount"] == AISubscriptionService.plan_amount_ars("vendedor")
    assert payment.preference_id == "ai-qr-pref-01"
    assert payment.status == "pending"
    assert payment.payment_method == "mercadopago_ai_qr"
    assert AISubscriptionService.get_status(company)["status"] is None
    assert _standard_snapshot(subscription) == before


def test_cancel_ai_qr_checkout_does_not_change_either_subscription(subscription_app, monkeypatch):
    company, user, _, subscription = _tenant_with_standard_subscription()
    before = _standard_snapshot(subscription)
    draft = Payment(
        company_id=company.id,
        user_id=user.id,
        subscription_id=None,
        amount=AISubscriptionService.plan_amount_ars("inicio"),
        currency="ARS",
        status="pending",
        payment_method="mercadopago_ai_qr",
        provider="mercadopago_ai_qr",
        reference="ai-qr:test-cancel",
        external_reference=f"ai_subscription_qr:true|flow:ai_subscription_qr|company_id:{company.id}|plan_code:inicio|payment_record_id:999|user_id:{user.id}|nonce:cancel",
        payload_json='{"id":"ai-qr-pref-cancel","init_point":"https://mp.test/ai-qr-cancel"}',
        preference_id="ai-qr-pref-cancel",
    )
    db.session.add(draft)
    db.session.flush()
    ref_parts = draft.external_reference.replace("payment_record_id:999", f"payment_record_id:{draft.id}")
    draft.external_reference = ref_parts
    db.session.commit()
    client = subscription_app.test_client()
    _login(client, user)

    response = client.post(
        "/admin/subscription/ai-agent/qr/cancel",
        data={"payment_record_id": draft.id},
    )

    db.session.refresh(draft)
    assert response.status_code == 302
    assert draft.status == "cancelled"
    assert AISubscriptionService.get_status(company)["status"] is None
    assert _standard_snapshot(subscription) == before


def test_ai_qr_approved_webhook_activates_manual_plan_without_touching_standard(subscription_app, monkeypatch):
    company, user, _, subscription = _tenant_with_standard_subscription()
    before = _standard_snapshot(subscription)
    amount = AISubscriptionService.plan_amount_ars("inicio")
    draft = Payment(
        company_id=company.id,
        user_id=user.id,
        amount=amount,
        currency="ARS",
        status="pending",
        payment_method="mercadopago_ai_qr",
        provider="mercadopago_ai_qr",
        reference="ai-qr:test-webhook",
        payload_json='{"id":"ai-qr-pref-webhook","init_point":"https://mp.test/ai-qr-webhook"}',
        preference_id="ai-qr-pref-webhook",
    )
    db.session.add(draft)
    db.session.flush()
    draft.external_reference = (
        f"ai_subscription_qr:true|flow:ai_subscription_qr|company_id:{company.id}|"
        f"plan_code:inicio|payment_record_id:{draft.id}|user_id:{user.id}|nonce:webhook"
    )
    db.session.commit()
    payment_data = {
        "id": "mp-ai-qr-payment-01",
        "status": "approved",
        "external_reference": draft.external_reference,
        "metadata": {
            "flow": "ai_subscription_qr",
            "company_id": company.id,
            "plan_code": "inicio",
            "payment_record_id": draft.id,
            "user_id": user.id,
        },
        "transaction_amount": amount,
        "currency_id": "ARS",
        "date_approved": "2026-10-09T12:00:00Z",
        "date_last_updated": "2026-10-09T12:00:00Z",
    }
    service = WebhookService()
    monkeypatch.setattr(service.mp_service, "validate_webhook_signature", lambda **kwargs: True)
    monkeypatch.setattr(service.mp_service, "get_payment", lambda data_id: payment_data)

    result = service.process(
        db_session=db.session,
        headers={"x-request-id": "rq-ai-qr", "x-signature": "ts=1,v1=abc"},
        payload={"id": "evt-ai-qr-01", "type": "payment", "data": {"id": "mp-ai-qr-payment-01"}},
    )

    status = AISubscriptionService.get_status(company)
    db.session.refresh(draft)
    assert result["status"] == "processed_ai_qr_subscription_payment"
    assert draft.status == "approved"
    assert status["status"] == "ACTIVA"
    assert status["origin"] == "MANUAL"
    assert status["checkout_method"] == "qr"
    assert status["mercadopago_preapproval_id"] is None
    assert status["last_payment_id"] == "mp-ai-qr-payment-01"
    assert status["ends_at"]
    assert _standard_snapshot(subscription) == before



def test_late_approved_webhook_refunds_terminal_ai_qr_checkout_without_activation(subscription_app, monkeypatch):
    company, user, _, subscription = _tenant_with_standard_subscription()
    before = _standard_snapshot(subscription)
    amount = AISubscriptionService.plan_amount_ars("inicio")
    draft = Payment(
        company_id=company.id,
        user_id=user.id,
        amount=amount,
        currency="ARS",
        status="refunded",
        payment_method="mercadopago_ai_qr",
        provider="mercadopago_ai_qr",
        reference="ai-qr:late-refund",
        preference_id="ai-qr-pref-late",
    )
    db.session.add(draft)
    db.session.flush()
    draft.external_reference = (
        f"ai_subscription_qr:true|flow:ai_subscription_qr|company_id:{company.id}|"
        f"plan_code:inicio|payment_record_id:{draft.id}|user_id:{user.id}|nonce:late"
    )
    db.session.commit()
    payment_data = {
        "id": "mp-ai-qr-late-approved",
        "status": "approved",
        "external_reference": draft.external_reference,
        "metadata": {
            "flow": "ai_subscription_qr",
            "company_id": company.id,
            "plan_code": "inicio",
            "payment_record_id": draft.id,
            "user_id": user.id,
        },
        "transaction_amount": amount,
        "currency_id": "ARS",
        "date_approved": "2026-10-09T12:00:00Z",
    }
    service = WebhookService()
    refunded = []
    monkeypatch.setattr(service.mp_service, "validate_webhook_signature", lambda **kwargs: True)
    monkeypatch.setattr(service.mp_service, "get_payment", lambda data_id: payment_data)
    monkeypatch.setattr(
        service.mp_service,
        "refund_payment",
        lambda payment_id: refunded.append(payment_id) or {"id": "refund-ai-qr-late", "status": "approved"},
    )

    result = service.process(
        db_session=db.session,
        headers={"x-request-id": "rq-ai-qr-late", "x-signature": "ts=1,v1=abc"},
        payload={"id": "evt-ai-qr-late", "type": "payment", "data": {"id": "mp-ai-qr-late-approved"}},
    )

    db.session.refresh(draft)
    assert result["status"] == "cancelled_ai_qr_payment_refunded"
    assert refunded == ["mp-ai-qr-late-approved"]
    assert draft.status == "refunded"
    assert AISubscriptionService.get_status(company)["status"] is None
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
    monkeypatch.setattr(service.mp_service, "get_preapproval", lambda data_id: {"id": "standard-pre", "status": "authorized", "next_payment_date": "2099-10-09T12:00:00Z", "external_reference": f"stockarmobile|flow:subscription_auto|company_id:{company.id}|subscription_id:{subscription.id}|nonce:def"})

    result = service.process(db_session=db.session, headers={"x-request-id": "rq-standard", "x-signature": "ts=1,v1=abc"}, payload={"id": "evt-standard", "type": "subscription_preapproval", "data": {"id": "standard-pre"}})

    db.session.refresh(subscription)
    assert result["status"] == "processed"
    assert result["subscription_id"] == subscription.id
    assert subscription.status == SubscriptionService.STATE_ACTIVE
    assert subscription.auto_renew is True
    assert AISubscriptionService.get_status(company) == ai_before


def test_standard_monthly_plan_change_switches_only_after_new_preapproval_authorized(subscription_app, monkeypatch):
    company, user, _, old_subscription = _tenant_with_standard_subscription()
    SubscriptionService._set_metadata(
        old_subscription,
        {
            "mercadopago_preapproval_id": "old-standard-preapproval",
            "mercadopago_status": "authorized",
            "payment_method": "mercadopago_subscription",
            "checkout_method": "automatic",
        },
    )
    new_plan = Plan(
        code="standard_monthly_switch_test",
        name="Standard Monthly Switch Test",
        price=2500,
        currency="ARS",
        duration_days=30,
        active=True,
    )
    db.session.add(new_plan)
    db.session.commit()

    result = SubscriptionService.run_command(
        db.session,
        SubscriptionService.ChangePlanCommand(
            company_id=company.id,
            plan_id=new_plan.id,
            actor_user_id=user.id,
            actor_role=user.role,
            origin="portal_confirm",
            idempotency_key="test-monthly-plan-switch",
        ),
    )
    new_subscription = db.session.get(Subscription, result.subscription_id)
    assert new_subscription is not None
    pending_metadata = SubscriptionService._metadata_dict(new_subscription)
    assert pending_metadata["pending_plan_change"] is True
    assert pending_metadata["previous_subscription_id"] == old_subscription.id
    SubscriptionService._set_metadata(
        new_subscription,
        {
            "mercadopago_preapproval_id": "new-standard-preapproval",
            "mercadopago_status": "pending",
            "payment_method": "mercadopago_subscription",
            "checkout_method": "automatic",
        },
    )
    db.session.commit()

    calls = []

    def get_preapproval(self, preapproval_id):
        calls.append(("get", preapproval_id))
        return {"id": preapproval_id, "status": "authorized"}

    def cancel_preapproval(self, preapproval_id):
        calls.append(("cancel", preapproval_id))
        return {"id": preapproval_id, "status": "cancelled"}

    monkeypatch.setattr("services.mercadopago_service.MercadoPagoService.get_preapproval", get_preapproval)
    monkeypatch.setattr("services.mercadopago_service.MercadoPagoService.cancel_preapproval", cancel_preapproval)

    from services.mercadopago_subscription_service import MercadoPagoSubscriptionService

    synced = MercadoPagoSubscriptionService.sync_preapproval(
        db_session=db.session,
        preapproval={
            "id": "new-standard-preapproval",
            "status": "authorized",
            "next_payment_date": "2099-10-09T12:00:00Z",
        },
    )
    db.session.commit()
    db.session.refresh(old_subscription)
    db.session.refresh(new_subscription)

    new_metadata = SubscriptionService._metadata_dict(new_subscription)
    old_metadata = SubscriptionService._metadata_dict(old_subscription)
    assert synced is new_subscription
    assert calls == [("get", "old-standard-preapproval"), ("cancel", "old-standard-preapproval")]
    assert old_subscription.status == SubscriptionService.STATE_CANCELLED
    assert old_subscription.auto_renew is False
    assert old_subscription.renewal_enabled is False
    assert old_metadata["mercadopago_status"] == "cancelled"
    assert new_subscription.status == SubscriptionService.STATE_ACTIVE
    assert new_subscription.auto_renew is True
    assert new_subscription.renewal_enabled is True
    assert new_metadata["pending_plan_change"] is False
    assert new_metadata["replaced_subscription_id"] == old_subscription.id


def test_standard_qr_checkout_can_be_cancelled_without_cancelling_active_subscription(subscription_app):
    company, user, _, subscription = _tenant_with_standard_subscription()
    SubscriptionService._set_metadata(subscription, {"checkout_method": "qr"})
    from services.billing_notification_service import NotificationService

    NotificationService.record_event(
        db.session,
        company_id=company.id,
        subscription_id=subscription.id,
        event="checkout_preference_created",
        detail="Pending standard QR",
        source="mercadopago",
        status="pending",
        event_id="standard-qr-cancel",
        payload={"id": "standard-qr-cancel", "init_point": "https://mp.test/standard-qr-cancel"},
        user_id=user.id,
    )
    db.session.commit()

    client = subscription_app.test_client()
    _login(client, user)
    response = client.post(
        "/admin/subscription/checkout/cancel",
        data={"subscription_id": subscription.id},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert "#payment-checkout" in response.headers["Location"]
    db.session.refresh(subscription)
    metadata = SubscriptionService._metadata_dict(subscription)
    assert subscription.status == SubscriptionService.STATE_ACTIVE
    assert metadata.get("checkout_method") is None
    assert metadata["checkout_cancelled"] is True
    assert _standard_snapshot(subscription)["status"] == SubscriptionService.STATE_ACTIVE


def test_pending_plan_change_checkout_can_be_cancelled_without_disabling_current_plan(subscription_app):
    company, user, current_plan, active_subscription = _tenant_with_standard_subscription()
    target_plan = Plan(code="premium_cancel_checkout", name="Premium", price=54999, currency="ARS", duration_days=30, active=True)
    db.session.add(target_plan)
    db.session.flush()

    result = SubscriptionService.run_command(
        db.session,
        SubscriptionService.ChangePlanCommand(
            company_id=company.id,
            plan_id=target_plan.id,
            actor_user_id=user.id,
            actor_role=user.role,
            origin="portal_confirm",
            idempotency_key="cancel-pending-plan-checkout",
        ),
    )
    pending = db.session.get(Subscription, result.subscription_id)
    assert pending.status == SubscriptionService.STATE_PENDING_PAYMENT
    assert SubscriptionService._metadata_dict(pending).get("pending_plan_change") is True
    db.session.commit()

    client = subscription_app.test_client()
    _login(client, user)
    response = client.post(
        "/admin/subscription/checkout/cancel",
        data={"subscription_id": pending.id},
        follow_redirects=False,
    )

    assert response.status_code == 302
    db.session.refresh(pending)
    db.session.refresh(active_subscription)
    db.session.refresh(company)
    metadata = SubscriptionService._metadata_dict(pending)
    assert pending.status == SubscriptionService.STATE_CANCELLED
    assert metadata["checkout_cancelled"] is True
    assert metadata["pending_plan_change"] is False
    assert active_subscription.id != pending.id
    assert active_subscription.status == SubscriptionService.STATE_ACTIVE
    assert company.active is True


def test_ai_pending_checkout_can_be_cancelled_from_checkout_without_affecting_standard(subscription_app, monkeypatch):
    company, user, _, subscription = _tenant_with_standard_subscription()
    before = _standard_snapshot(subscription)
    update_ai_preferences(
        company,
        ai_updates={
            "plan_code": "inicio",
            "status": "PENDIENTE",
            "origin": "MERCADO_PAGO",
            "mercadopago_preapproval_id": "ai-pre-cancel-checkout",
            "checkout_method": "qr",
        },
    )
    db.session.commit()

    monkeypatch.setattr(
        AISubscriptionService,
        "_get_mp_preapproval",
        lambda company: {"id": "ai-pre-cancel-checkout", "status": "cancelled"},
    )
    client = subscription_app.test_client()
    _login(client, user)
    response = client.post(
        "/admin/subscription/ai-agent/cancel",
        data={"confirm_ai_cancel": "1", "from_checkout": "1"},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert "#payment-checkout" in response.headers["Location"]
    assert AISubscriptionService.get_status(company)["status"] == "CANCELADA"
    assert _standard_snapshot(subscription) == before


def test_late_approved_superseded_checkout_is_refunded_without_disabling_company(subscription_app, monkeypatch):
    company, user, _, active_subscription = _tenant_with_standard_subscription()
    premium = Plan(code="premium_stale", name="Premium", price=54999, currency="ARS", duration_days=30, active=True)
    db.session.add(premium)
    db.session.flush()
    result = SubscriptionService.run_command(
        db.session,
        SubscriptionService.ChangePlanCommand(
            company_id=company.id,
            plan_id=premium.id,
            actor_user_id=user.id,
            actor_role=user.role,
            origin="portal_confirm",
            idempotency_key="stale-premium-checkout",
        ),
    )
    old_subscription = db.session.get(Subscription, result.subscription_id)
    SubscriptionService._close_for_change(
        old_subscription,
        now=stock_app.utcnow(),
        actor_user_id=user.id,
        origin="portal_confirm_replaced",
    )
    SubscriptionService._set_metadata(old_subscription, {"pending_plan_change": False, "checkout_superseded": True})
    company.active = True
    payment = Payment(
        payment_id="123456789",
        preference_id="old-premium-preference",
        external_reference=(
            f"company_id:{company.id}|plan_id:{premium.id}|subscription_id:{old_subscription.id}|"
            f"user_id:{user.id}|checkout_attempt:1"
        ),
        company_id=company.id,
        subscription_id=old_subscription.id,
        user_id=user.id,
        amount=54999,
        currency="ARS",
        status="pending",
        provider="mercadopago",
    )
    db.session.add(payment)
    db.session.commit()
    payment_data = {
        "id": payment.payment_id,
        "status": "approved",
        "external_reference": payment.external_reference,
        "transaction_amount": 54999,
        "currency_id": "ARS",
        "date_approved": "2026-10-07T20:00:00Z",
        "date_last_updated": "2026-10-07T20:00:00Z",
        "payment_method_id": "account_money",
        "order": {"id": payment.preference_id},
        "metadata": {"company_id": company.id, "plan_id": premium.id, "user_id": user.id},
    }
    service = WebhookService()
    refunds = []
    monkeypatch.setattr(service.mp_service, "validate_webhook_signature", lambda **kwargs: True)
    monkeypatch.setattr(service.mp_service, "get_payment", lambda payment_id, **kwargs: payment_data)
    monkeypatch.setattr(
        service.mp_service,
        "refund_payment",
        lambda payment_id: refunds.append(payment_id) or {"id": "refund-1", "status": "approved"},
    )

    response = service.process(
        db_session=db.session,
        headers={"x-request-id": "rq-stale", "x-signature": "ts=1,v1=abc"},
        payload={"id": "evt-stale", "type": "payment", "data": {"id": payment.payment_id}},
    )

    db.session.refresh(payment)
    db.session.refresh(old_subscription)
    db.session.refresh(active_subscription)
    db.session.refresh(company)
    assert response["status"] == "stale_payment_refunded"
    assert refunds == [payment.payment_id]
    assert payment.status == "refunded"
    assert old_subscription.status == SubscriptionService.STATE_CANCELLED
    assert active_subscription.status == SubscriptionService.STATE_ACTIVE
    assert company.active is True


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
    admin = User(username="super_ai", email="super_ai@test.local", role="superadmin", active=True)
    admin.set_password("admin123")
    db.session.add(admin)
    db.session.commit()
    before = _standard_snapshot(subscription)
    client = subscription_app.test_client()
    _login(client, admin)

    response = client.post(
        f"/superadmin/ai-subscriptions/{company.id}/action",
        data={"action": "assign_plan", "plan_code": "pro", "step_up_password": "admin123"},
    )

    assert response.status_code == 302
    status = AISubscriptionService.get_status(company)
    assert status["plan_code"] == "pro"
    assert status["status"] == "ACTIVA"
    assert status["origin"] == "MANUAL"
    assert _standard_snapshot(subscription) == before



def test_downgrade_business_to_entrepreneur_12999_creates_single_pending_checkout(subscription_app, monkeypatch):
    from services.billing_service import BillingService

    company = Company(name="Empresa Downgrade", active=True, contact_email="downgrade@test.local")
    db.session.add(company)
    db.session.flush()
    user = User(
        username="downgrade_admin",
        email="downgrade@test.local",
        password_hash="x",
        role="admin",
        active=True,
        company_id=company.id,
    )
    business = Plan(code="business", name="Business", price=29999, currency="ARS", duration_days=30, active=True)
    entrepreneur = Plan(code="entrepreneur", name="Entrepreneur", price=12999, currency="ARS", duration_days=30, active=True)
    db.session.add_all([user, business, entrepreneur])
    db.session.flush()
    current = Subscription(
        company_id=company.id,
        plan_id=business.id,
        status=SubscriptionService.STATE_ACTIVE,
        renewal_enabled=True,
        auto_renew=True,
    )
    db.session.add(current)
    db.session.commit()

    calls = []
    def fake_checkout(self, **kwargs):
        calls.append(kwargs)
        return {
            "subscription": kwargs["subscription"],
            "preference": {
                "id": "pref-downgrade-12999",
                "init_point": "https://mp.test/downgrade-12999",
            },
        }

    monkeypatch.setattr(BillingService, "create_checkout_for_plan", fake_checkout)

    client = subscription_app.test_client()
    _login(client, user)

    first = client.post("/admin/subscription/change", data={"plan_id": entrepreneur.id}, follow_redirects=False)
    second = client.post("/admin/subscription/change", data={"plan_id": entrepreneur.id}, follow_redirects=False)

    assert first.status_code == second.status_code == 302
    assert first.headers["Location"] == second.headers["Location"]
    assert "#payment-checkout" in first.headers["Location"]

    rows = Subscription.query.filter_by(company_id=company.id).order_by(Subscription.id.asc()).all()
    assert len(rows) == 2
    assert rows[0].plan_id == business.id
    assert rows[0].status == SubscriptionService.STATE_ACTIVE
    assert rows[1].plan_id == entrepreneur.id
    assert rows[1].status == SubscriptionService.STATE_PENDING_PAYMENT
    assert len(calls) == 2


def test_subscription_portal_has_one_real_payment_anchor():
    from pathlib import Path
    html = Path("templates/company_billing/portal.html").read_text(encoding="utf-8")
    assert html.count('<section id="payment-checkout"') == 1
    assert 'id="payment-checkout-status"' in html
    assert 'name="payment_method" value="qr"' in html
    assert 'Pagar con QR' in html
    assert 'Pago mensual con QR' in html
    assert 'El próximo ciclo no se cobra solo.' in html
    assert 'Cobro automático mensual' in html
    assert 'Cancelar checkout' in html
    assert 'company_billing.cancel_checkout' in html
    assert 'company_billing.cancel_ai_subscription' in html
    assert 'history.replaceState' in html



def test_subscription_portal_renders_after_checkout_with_usage_snapshot_dict(subscription_app):
    company, user, _, subscription = _tenant_with_standard_subscription()
    subscription.status = SubscriptionService.STATE_ACTIVE
    db.session.commit()

    client = subscription_app.test_client()
    _login(client, user)

    response = client.get("/admin/portal?checkout=created&checkout_subscription_id=" + str(subscription.id))
    assert response.status_code == 200
    assert "Mi Suscripción" in response.get_data(as_text=True)
