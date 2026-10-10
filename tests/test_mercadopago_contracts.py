import os
from types import SimpleNamespace

os.environ.setdefault("MP_MODE", "sandbox")
os.environ.setdefault("MP_ACCESS_TOKEN", "test-token")
os.environ.setdefault("MP_WEBHOOK_SECRET", "test-secret")

from services.mercadopago_service import MercadoPagoService
from services.mercadopago_subscription_service import MercadoPagoSubscriptionService


def test_preapproval_explicitly_uses_pending_flow(monkeypatch):
    service = MercadoPagoService()
    captured = {}

    def fake_request(method, path, *, payload=None, access_token=None, idempotency_key=None):
        captured.update(
            {
                "method": method,
                "path": path,
                "payload": payload,
                "idempotency_key": idempotency_key,
            }
        )
        return {"id": "preapproval-test", "status": "pending", "init_point": "https://example.test/subscription"}

    monkeypatch.setattr(service, "_request", fake_request)

    response = service.create_preapproval(
        reason="StockArMobile - Plan Negocio",
        payer_email="cliente@example.com",
        external_reference="stockarmobile|company_id:1|subscription_id:2",
        amount=29999,
        currency="ARS",
        frequency=1,
        frequency_type="months",
        notification_url="https://www.stockarmobile.com/admin/webhooks/mercadopago",
        back_url="https://www.stockarmobile.com/admin/portal?checkout=success",
    )

    assert response["status"] == "pending"
    assert captured["method"] == "POST"
    assert captured["path"] == "/preapproval"
    assert captured["payload"]["status"] == "pending"
    assert captured["payload"]["auto_recurring"]["frequency"] == 1
    assert captured["payload"]["auto_recurring"]["frequency_type"] == "months"
    assert captured["payload"]["auto_recurring"]["transaction_amount"] == 29999.0
    assert captured["payload"]["auto_recurring"]["currency_id"] == "ARS"


def test_ai_qr_preference_is_one_time_checkout_not_preapproval(monkeypatch):
    service = MercadoPagoService()
    captured = {}

    def fake_request(method, path, *, payload=None, access_token=None, idempotency_key=None):
        captured.update(
            method=method,
            path=path,
            payload=payload,
            idempotency_key=idempotency_key,
        )
        return {"id": "ai-qr-pref-1", "init_point": "https://www.mercadopago.com.ar/checkout/v1/redirect?pref_id=ai-qr-pref-1"}

    monkeypatch.setattr(service, "_request", fake_request)
    response = service.create_ai_subscription_qr_preference(
        title="StockArMobile IA - Inicio",
        amount=4999,
        external_reference="ai_subscription_qr:true|flow:ai_subscription_qr|company_id:1|plan_code:inicio|payment_record_id:9|user_id:2|nonce:test",
        company_id=1,
        plan_code="inicio",
        payment_record_id=9,
        user_id=2,
    )

    assert response["id"] == "ai-qr-pref-1"
    assert captured["method"] == "POST"
    assert captured["path"] == "/checkout/preferences"
    assert captured["payload"]["items"][0]["unit_price"] == 4999.0
    assert captured["payload"]["items"][0]["currency_id"] == "ARS"
    assert captured["payload"]["metadata"] == {
        "flow": "ai_subscription_qr",
        "company_id": 1,
        "plan_code": "inicio",
        "payment_record_id": 9,
        "user_id": 2,
        "recurring": False,
    }
    assert "auto_recurring" not in captured["payload"]
    assert "no renueva automáticamente" in captured["payload"]["items"][0]["description"].lower()


def test_cancel_preapproval_uses_mercado_pago_cancelled_status(monkeypatch):
    service = MercadoPagoService()
    captured = {}

    def fake_request(method, path, *, payload=None, access_token=None, idempotency_key=None):
        captured.update(
            {
                "method": method,
                "path": path,
                "payload": payload,
                "idempotency_key": idempotency_key,
            }
        )
        return {"id": "preapproval-test", "status": "cancelled"}

    monkeypatch.setattr(service, "_request", fake_request)

    response = service.cancel_preapproval("preapproval-test")

    assert response["status"] == "cancelled"
    assert captured["method"] == "PUT"
    assert captured["path"] == "/preapproval/preapproval-test"
    assert captured["payload"] == {"status": "cancelled"}
    assert captured["idempotency_key"] == "preapproval-update:preapproval-test:cancelled"


def test_superseded_plan_payment_refund_uses_idempotent_full_refund_endpoint(monkeypatch):
    service = MercadoPagoService()
    captured = {}

    def fake_request(method, path, *, payload=None, access_token=None, idempotency_key=None):
        captured.update(method=method, path=path, payload=payload, idempotency_key=idempotency_key)
        return {"id": "refund-test", "status": "approved"}

    monkeypatch.setattr(service, "_request", fake_request)

    response = service.refund_payment("123456789")

    assert response["status"] == "approved"
    assert captured == {
        "method": "POST",
        "path": "/v1/payments/123456789/refunds",
        "payload": None,
        "idempotency_key": "superseded-plan-checkout-refund:123456789",
    }


def test_standard_preapproval_without_init_point_reuses_existing_pending_contract(monkeypatch):
    calls = []
    company = SimpleNamespace(id=1)
    plan = SimpleNamespace(id=2, name="Standard", price=1000, currency="ARS")
    subscription = SimpleNamespace(id=3, metadata_json='{"mercadopago_preapproval_id":"old-pre"}', renewal_enabled=True, auto_renew=True, cancel_at_period_end=False)
    db_session = SimpleNamespace(
        flush=lambda: calls.append(("flush", None)),
        commit=lambda: calls.append(("commit", None)),
    )

    def get_preapproval(self, preapproval_id):
        calls.append(("get", preapproval_id))
        return {"id": preapproval_id, "status": "pending"}

    def create_preapproval(self, **kwargs):
        raise AssertionError("A pending preapproval must be reused; missing init_point must not create a duplicate.")

    monkeypatch.setattr("services.mercadopago_service.MercadoPagoService.get_preapproval", get_preapproval)
    monkeypatch.setattr("services.mercadopago_service.MercadoPagoService.create_preapproval", create_preapproval)

    response = MercadoPagoSubscriptionService.create(
        db_session=db_session,
        company=company,
        subscription=subscription,
        plan=plan,
        payer_email="cliente@example.com",
        notification_url="https://www.stockarmobile.com/admin/webhooks/mercadopago",
        back_url="https://www.stockarmobile.com/admin/portal?checkout=success",
    )

    assert response["id"] == "old-pre"
    assert response["init_point"] == "https://www.mercadopago.com.ar/subscriptions/checkout?preapproval_id=old-pre"
    assert calls == [("get", "old-pre")]


def test_authorized_standard_preapproval_without_init_point_is_reused(monkeypatch):
    calls = []
    subscription = SimpleNamespace(
        id=3,
        status="pending",
        metadata_json='{"mercadopago_preapproval_id":"already-authorized"}',
        renewal_enabled=False,
        auto_renew=False,
        cancel_at_period_end=True,
        next_billing_date=None,
        ends_at=None,
    )
    db_session = SimpleNamespace(flush=lambda: calls.append("flush"), commit=lambda: calls.append("commit"))

    def get_preapproval(self, preapproval_id):
        calls.append(("get", preapproval_id))
        return {
            "id": preapproval_id,
            "status": "authorized",
            "next_payment_date": "2099-10-01T00:00:00Z",
        }

    def create_preapproval(self, **kwargs):
        raise AssertionError("An authorized recurring subscription must never be duplicated.")

    monkeypatch.setattr("services.mercadopago_service.MercadoPagoService.get_preapproval", get_preapproval)
    monkeypatch.setattr("services.mercadopago_service.MercadoPagoService.create_preapproval", create_preapproval)

    response = MercadoPagoSubscriptionService.create(
        db_session=db_session,
        company=SimpleNamespace(id=1),
        subscription=subscription,
        plan=SimpleNamespace(id=2, name="Standard", price=1000, currency="ARS"),
        payer_email="cliente@example.com",
        notification_url="https://www.stockarmobile.com/admin/webhooks/mercadopago",
        back_url="https://www.stockarmobile.com/admin/portal",
    )

    assert response["id"] == "already-authorized"
    assert response["status"] == "authorized"
    assert calls == [("get", "already-authorized")]
    assert subscription.status == "active"
    assert subscription.renewal_enabled is True
    assert subscription.auto_renew is True
    assert subscription.cancel_at_period_end is False
    assert subscription.next_billing_date.isoformat() == "2099-10-01T00:00:00"
    assert subscription.ends_at == subscription.next_billing_date


def test_webhook_signature_matches_mercado_pago_manifest():
    import hashlib
    import hmac

    service = MercadoPagoService()
    request_id = "request-123"
    data_id = "payment-456"
    ts = "1720000000"
    manifest = f"id:{data_id};request-id:{request_id};ts:{ts};"
    digest = hmac.new(b"test-secret", manifest.encode(), hashlib.sha256).hexdigest()

    assert service.validate_webhook_signature(
        request_id=request_id,
        x_signature=f"ts={ts},v1={digest}",
        data_id=data_id,
    )
    assert not service.validate_webhook_signature(
        request_id=request_id,
        x_signature=f"ts={ts},v1={'0' * 64}",
        data_id=data_id,
    )
