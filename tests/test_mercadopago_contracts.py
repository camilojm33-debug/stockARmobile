import os

os.environ.setdefault("MP_MODE", "sandbox")
os.environ.setdefault("MP_ACCESS_TOKEN", "test-token")
os.environ.setdefault("MP_WEBHOOK_SECRET", "test-secret")

from services.mercadopago_service import MercadoPagoService


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
