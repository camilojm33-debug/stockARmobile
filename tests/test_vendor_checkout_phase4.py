from types import SimpleNamespace

from flask import Flask

from services.ai_agent import vendor_checkout_public


def test_public_mutation_guard_rejects_oversized_payload():
    app = Flask(__name__)
    with app.test_request_context(
        "/vendedor/demo/checkout",
        method="POST",
        data="x" * (vendor_checkout_public.PUBLIC_POST_MAX_BYTES + 1),
        content_type="application/json",
    ):
        response = vendor_checkout_public._guard_public_mutations()
        assert response is not None
        assert response[1] == 413


def test_public_mutation_guard_rate_limits_checkout(monkeypatch):
    app = Flask(__name__)
    app.config["TESTING"] = True
    company = SimpleNamespace(id=77)
    monkeypatch.setattr(vendor_checkout_public, "_public_available_company", lambda slug: company)
    monkeypatch.setattr(vendor_checkout_public, "_rate_limit", lambda company_id: False)

    with app.test_request_context(
        "/vendedor/demo/checkout",
        method="POST",
        json={"conversation_id": 1},
    ):
        response = vendor_checkout_public._guard_public_mutations()
        assert response is not None
        assert response[1] == 429
        assert response[2]["Retry-After"] == "60"


def test_public_order_status_without_conversation_is_empty(monkeypatch):
    app = Flask(__name__)
    app.config["TESTING"] = True
    company = SimpleNamespace(id=12)
    monkeypatch.setattr(vendor_checkout_public, "_public_available_company", lambda slug: company)
    monkeypatch.setattr(
        vendor_checkout_public,
        "can_use_ai",
        lambda company, agent: SimpleNamespace(allowed=True, reason=""),
    )
    monkeypatch.setattr(vendor_checkout_public, "_public_conversation", lambda *args, **kwargs: None)

    with app.test_request_context("/vendedor/demo/order/status?conversation_id=999"):
        response = vendor_checkout_public.public_vendor_order_status("demo")
        assert response.status_code == 200
        assert response.get_json() == {"success": True, "found": False, "conversation_id": None}


def test_cancel_requires_explicit_confirmation(monkeypatch):
    app = Flask(__name__)
    app.config["TESTING"] = True
    company = SimpleNamespace(id=12)
    conversation = SimpleNamespace(id=44)
    captured = {}

    monkeypatch.setattr(vendor_checkout_public, "_public_available_company", lambda slug: company)
    monkeypatch.setattr(
        vendor_checkout_public,
        "can_use_ai",
        lambda company, agent: SimpleNamespace(allowed=True, reason=""),
    )
    monkeypatch.setattr(vendor_checkout_public, "_public_conversation", lambda *args, **kwargs: conversation)
    monkeypatch.setattr(vendor_checkout_public, "_cart_public_state", lambda conversation: {"cart": {"items": [], "total": 0}})
    monkeypatch.setattr(vendor_checkout_public.VendorOrderService, "cancel_order", lambda **kwargs: captured.update(kwargs) or {"success": False, "confirmation_required": True})

    with app.test_request_context(
        "/vendedor/demo/order/cancel",
        method="POST",
        json={"conversation_id": 44, "order_number": "P-000001", "confirm": False},
    ):
        response = vendor_checkout_public.public_vendor_cancel_order("demo")
        assert response.get_json()["confirmation_required"] is True
        assert captured["confirm"] is False
