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


def test_standard_preapproval_without_init_point_is_not_reused(monkeypatch):
    calls = []
    company = SimpleNamespace(id=1)
    plan = SimpleNamespace(id=2, name="Standard", price=1000, currency="ARS")
    subscription = SimpleNamespace(id=3, metadata_json='{"mercadopago_preapproval_id":"old-pre"}', renewal_enabled=True, auto_renew=True, cancel_at_period_end=False)
    db_session = SimpleNamespace(flush=lambda: calls.append(("flush", None)))

    def get_preapproval(self, preapproval_id):
        calls.append(("get", preapproval_id))
        return {"id": preapproval_id, "status": "pending"}

    def create_preapproval(self, **kwargs):
        calls.append(("create", kwargs))
        return {"id": "new-pre", "status": "pending", "init_point": "https://mp.test/new"}

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

    assert response["id"] == "new-pre"
    assert response["init_point"] == "https://mp.test/new"
    assert [kind for kind, _ in calls] == ["get", "create", "flush"]


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
