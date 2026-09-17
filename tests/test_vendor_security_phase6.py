from types import SimpleNamespace

from flask import Flask

from services.ai_agent import vendor_checkout_public


def test_public_mutation_guard_uses_company_from_slug_not_payload(monkeypatch):
    app = Flask(__name__)
    app.config["TESTING"] = True
    company = SimpleNamespace(id=77)
    conversation = SimpleNamespace(id=44)
    captured = {}

    monkeypatch.setattr(vendor_checkout_public, "_public_available_company", lambda slug: company)
    monkeypatch.setattr(vendor_checkout_public, "_rate_limit", lambda company_id: True)
    monkeypatch.setattr(
        vendor_checkout_public,
        "_public_conversation",
        lambda current_company, conversation_id, create=False: captured.update(
            company=current_company,
            conversation_id=conversation_id,
            create=create,
        ) or conversation,
    )
    monkeypatch.setattr(
        vendor_checkout_public,
        "_lock_public_conversation",
        lambda current_company, conversation_id: conversation,
    )

    with app.test_request_context(
        "/vendedor/demo/checkout",
        method="POST",
        json={"company_id": 9999, "conversation_id": 44, "total": 1},
    ):
        response = vendor_checkout_public._guard_public_mutations()

    assert response is None
    assert captured["company"].id == 77
    assert captured["conversation_id"] == 44
    assert captured["create"] is False


def test_public_mutation_guard_rejects_when_conversation_cannot_be_locked(monkeypatch):
    app = Flask(__name__)
    app.config["TESTING"] = True
    company = SimpleNamespace(id=12)
    conversation = SimpleNamespace(id=9)

    monkeypatch.setattr(vendor_checkout_public, "_public_available_company", lambda slug: company)
    monkeypatch.setattr(vendor_checkout_public, "_rate_limit", lambda company_id: True)
    monkeypatch.setattr(vendor_checkout_public, "_public_conversation", lambda *args, **kwargs: conversation)
    monkeypatch.setattr(vendor_checkout_public, "_lock_public_conversation", lambda *args, **kwargs: None)

    with app.test_request_context(
        "/vendedor/demo/cart",
        method="POST",
        json={"conversation_id": 9, "product_id": 1, "quantity": 1},
    ):
        response = vendor_checkout_public._guard_public_mutations()

    assert response is not None
    assert response[1] == 403


def test_public_order_context_rejects_cross_tenant_conversation(monkeypatch):
    company = SimpleNamespace(id=100)
    monkeypatch.setattr(vendor_checkout_public, "_public_conversation", lambda *args, **kwargs: None)

    app = Flask(__name__)
    with app.test_request_context("/vendedor/demo/order/retry", method="POST", json={"conversation_id": 500, "company_id": 999}):
        conversation, error = vendor_checkout_public._order_context(company, {"conversation_id": 500, "company_id": 999})

    assert conversation is None
    assert error[1] == 403
    assert error[0].get_json()["success"] is False
