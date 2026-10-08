from pathlib import Path
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

import pytest

from services.ai_agent.providers.gemini import GeminiProvider


def test_public_webchat_gemini_clamps_invalid_deadline_and_can_disable_retries():
    provider = GeminiProvider(timeout=8, max_retries=0)
    assert provider.timeout == 10
    assert provider.max_retries == 0


def test_public_webchat_runtime_is_time_bounded():
    source = Path("services/ai_agent/orchestrator_v2.py").read_text(encoding="utf-8")
    assert "PUBLIC_WEBCHAT_MAX_TOOL_TURNS = 1" in source
    assert "PUBLIC_WEBCHAT_PROVIDER_TIMEOUT = 25.0" in source
    assert "PUBLIC_WEBCHAT_MAX_OUTPUT_TOKENS = 1600" in source
    assert "PUBLIC_WEBCHAT_HISTORY_LIMIT = 8" in source
    assert "max_tool_turns=PUBLIC_WEBCHAT_MAX_TOOL_TURNS if is_public_webchat else None" in source
    assert "max_retries=0 if is_public_webchat else None" in source


def test_public_vendor_tool_failure_does_not_escape_tool_loop():
    source = Path("services/ai_agent/orchestrator_v2.py").read_text(encoding="utf-8")
    assert "except ValueError as exc:" in source
    assert '"retryable": False' in source
    assert '"retryable": True' in source
    assert "AI tool execution failed" in source


def test_public_vendor_provider_failures_return_json_status():
    source = Path("services/ai_agent/vendor_publication.py").read_text(encoding="utf-8")
    assert "except AIProviderError as exc:" in source
    assert 'jsonify({"success": False, "error": str(exc)})' in source
    assert "status_code not in {429, 503}" in source


def test_preparar_pedido_can_build_cart_from_direct_product_request(monkeypatch):
    from services.ai_agent.orchestrator_v2 import VendorOrderPreviewTool, VendorCartTool
    from services.ai_agent import orchestrator_v2

    calls = []

    def fake_get_cart(**kwargs):
        calls.append(("get_cart", kwargs))
        # A previous checkout is still present in this conversation.
        return {
            "items": [{"product_id": 42, "name": "Machimbre", "quantity": 5, "subtotal": 33000}],
            "total": 33000,
            "currency": "ARS",
            "line_count": 1,
        }

    def fake_update_cart(**kwargs):
        calls.append(("update_cart", kwargs))
        return {"success": True, "items": [{"product_id": 42, "quantity": 4}], "total": 26400, "currency": "ARS", "line_count": 1}

    def fake_create_pending_order(**kwargs):
        calls.append(("create_pending_order", kwargs))
        return {"success": True, "quote_number": "P-000042", "total": 26400}

    monkeypatch.setattr(orchestrator_v2.VendorOrderService, "get_cart", fake_get_cart)
    monkeypatch.setattr(orchestrator_v2.VendorOrderService, "update_cart", fake_update_cart)
    monkeypatch.setattr(orchestrator_v2.VendorOrderService, "create_pending_order", fake_create_pending_order)

    tool = VendorOrderPreviewTool(company_id=1, conversation_id=99, customer_phone="3655344393")
    result = tool.execute(
        product_query="machimbre",
        quantity=4,
        customer_name="Waldo Ricollini",
        customer_phone="3655344393",
        delivery_method="envio",
        delivery_address="Nueva Orleans 2332",
        delivery_city="Resistencia",
        delivery_province="Chaco",
    )

    assert result["success"] is True
    assert [item[0] for item in calls] == ["get_cart", "update_cart", "create_pending_order"]
    assert calls[1][1]["items"] == [{
        "product_query": "machimbre",
        "quantity": 4,
        "replace_quantity": True,
    }]
    assert calls[2][1]["customer_name"] == "Waldo Ricollini"
    assert calls[2][1]["delivery_method"] == "envio"


@pytest.fixture
def qa_public_vendor_setup(app):
    from app import Company, Product, User, db
    from services.ai_agent.config_service import ensure_default_agents, update_ai_preferences
    from services.ai_agent.vendor_publication import publish_vendor

    app.config["APP_URL"] = "http://localhost"
    app.config["SERVER_NAME"] = "localhost"
    app.config["IS_PRODUCTION_ENV"] = False
    company = Company(
        name="QA WebChat",
        active=True,
        preferences_json=json.dumps({"ai_agent": {"enabled": True, "plan_code": "vendedor"}}),
    )
    db.session.add(company)
    db.session.flush()
    user = User(
        username="qa_webchat_admin",
        email="qa_webchat_admin@test.local",
        password_hash="not-used",
        role="admin",
        active=True,
        company_id=company.id,
    )
    product = Product(
        barcode="MACH-001",
        name="Machimbre pino",
        price=6600,
        cost_price=4000,
        stock=20,
        min_stock=1,
        active=True,
        company_id=company.id,
        unit_measure="m",
    )
    db.session.add_all([user, product])
    db.session.flush()
    ensure_default_agents(company.id)
    update_ai_preferences(company, ai_updates={
        "vendor_options": {"standard_shipping_cost": "1500.00"},
    })
    with app.test_request_context("/"):
        publication = publish_vendor(company)
    db.session.commit()
    return {
        "app": app,
        "company": company,
        "user": user,
        "product": product,
        "slug": publication["slug"],
        "page_path": urlsplit(publication["url"]).path,
    }


@pytest.fixture
def qa_public_vendor_db(qa_public_vendor_setup):
    return qa_public_vendor_setup


class SequenceProvider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def generate(self, **kwargs):
        self.calls += 1
        if not self.responses:
            return {"content": "Respuesta recuperada.", "tool_call": None}
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _install_provider(monkeypatch, provider):
    from services.ai_agent.orchestrator_v2 import AgentRuntime

    monkeypatch.setattr(AgentRuntime, "provider", classmethod(lambda cls, **kwargs: provider))


def _public_client(setup):
    client = setup["app"].test_client()
    response = client.get(setup["page_path"])
    assert response.status_code == 200
    return client


def _post_message(client, setup, key, message, conversation_id=None):
    payload = {"message": message}
    if conversation_id is not None:
        payload["conversation_id"] = conversation_id
    return client.post(
        f"/vendedor/{setup['slug']}/message",
        headers={"Idempotency-Key": key},
        json=payload,
    )


def _count_rate_limit(monkeypatch):
    import ai_agents

    calls = []

    def allow(company_id, **kwargs):
        calls.append((company_id, kwargs))
        return True

    monkeypatch.setattr(ai_agents, "_public_vendor_rate_limit", allow)
    return calls


def test_same_idempotency_key_replays_one_chat_result_and_one_rate(qa_public_vendor_db, monkeypatch):
    from app import db
    from stockarmobile.models.conversations import Conversation, ConversationMessage, PublicVendorOperation

    provider = SequenceProvider([{"content": "Hola, soy el Vendedor IA.", "tool_call": None}])
    _install_provider(monkeypatch, provider)
    rate_calls = _count_rate_limit(monkeypatch)
    client = _public_client(qa_public_vendor_db)
    message = "Hola, necesito ayuda con un producto."

    first = _post_message(client, qa_public_vendor_db, "operation-chat-001", message)
    second = _post_message(
        client,
        qa_public_vendor_db,
        "operation-chat-001",
        message,
        conversation_id=first.json["conversation_id"],
    )

    assert first.status_code == second.status_code == 200
    assert first.json == second.json
    assert provider.calls == 1
    assert len(rate_calls) == 1
    assert Conversation.query.filter_by(company_id=qa_public_vendor_db["company"].id, channel="webchat").count() == 1
    assert ConversationMessage.query.filter_by(company_id=qa_public_vendor_db["company"].id, role="user").count() == 1
    assert ConversationMessage.query.filter_by(company_id=qa_public_vendor_db["company"].id, role="assistant").count() == 1
    assert PublicVendorOperation.query.filter_by(company_id=qa_public_vendor_db["company"].id).count() == 1


def test_new_key_for_same_text_is_a_new_operation(qa_public_vendor_db, monkeypatch):
    from app import db
    from stockarmobile.models.conversations import ConversationMessage

    provider = SequenceProvider([
        {"content": "Respuesta uno.", "tool_call": None},
        {"content": "Respuesta dos.", "tool_call": None},
    ])
    _install_provider(monkeypatch, provider)
    rate_calls = _count_rate_limit(monkeypatch)
    client = _public_client(qa_public_vendor_db)
    text = "¿Tienen machimbre?"
    first = _post_message(client, qa_public_vendor_db, "operation-chat-new-1", text)
    second = _post_message(
        client,
        qa_public_vendor_db,
        "operation-chat-new-2",
        text,
        conversation_id=first.json["conversation_id"],
    )

    assert first.status_code == second.status_code == 200
    assert first.json["assistant_message_id"] != second.json["assistant_message_id"]
    assert provider.calls == 2
    assert len(rate_calls) == 2
    assert ConversationMessage.query.filter_by(company_id=qa_public_vendor_db["company"].id, role="user").count() == 2


def test_two_simultaneous_posts_with_same_key_create_one_conversation_and_turn(qa_public_vendor_db, monkeypatch):
    from app import db
    from stockarmobile.models.conversations import Conversation, ConversationMessage

    started = threading.Event()
    release = threading.Event()
    provider = SequenceProvider([{"content": "Respuesta concurrente.", "tool_call": None}])

    def slow_generate(**kwargs):
        provider.calls += 1
        started.set()
        assert release.wait(timeout=5)
        return {"content": "Respuesta concurrente.", "tool_call": None}

    provider.generate = slow_generate
    _install_provider(monkeypatch, provider)
    _count_rate_limit(monkeypatch)
    first_client = _public_client(qa_public_vendor_db)
    cookie = first_client.get_cookie("session")
    second_client = qa_public_vendor_db["app"].test_client()
    if cookie is not None:
        second_client.set_cookie("session", cookie.value)

    def post(client):
        return _post_message(client, qa_public_vendor_db, "operation-chat-race", "Hola.")

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(post, first_client)
        assert started.wait(timeout=5)
        second_future = pool.submit(post, second_client)
        release.set()
        first = first_future.result(timeout=10)
        second = second_future.result(timeout=10)

    assert first.status_code == second.status_code == 200
    assert first.json == second.json
    assert provider.calls == 1
    assert Conversation.query.filter_by(company_id=qa_public_vendor_db["company"].id, channel="webchat").count() == 1
    assert ConversationMessage.query.filter_by(company_id=qa_public_vendor_db["company"].id, role="user").count() == 1
    assert ConversationMessage.query.filter_by(company_id=qa_public_vendor_db["company"].id, role="assistant").count() == 1


def test_same_key_retry_after_provider_timeout_does_not_reconsume_session_rate(qa_public_vendor_db, monkeypatch):
    from app import db
    from services.ai_agent.providers.base import AIProviderError
    from stockarmobile.models.conversations import ConversationMessage

    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setitem(qa_public_vendor_db["app"].config, "IS_PRODUCTION_ENV", False)
    provider = SequenceProvider([
        AIProviderError("timeout", status_code=503),
        {"content": "Reintento completado.", "tool_call": None},
    ])
    _install_provider(monkeypatch, provider)
    client = _public_client(qa_public_vendor_db)

    first = _post_message(client, qa_public_vendor_db, "operation-timeout-001", "Hola.")
    second = _post_message(client, qa_public_vendor_db, "operation-timeout-001", "Hola.")

    assert first.status_code == 503
    assert first.is_json
    assert second.status_code == 200
    assert second.json["content"] == "Reintento completado."
    assert provider.calls == 2
    assert ConversationMessage.query.filter_by(company_id=qa_public_vendor_db["company"].id, role="user").count() == 1


def test_public_rate_limit_returns_json_429_without_calling_provider(qa_public_vendor_db, monkeypatch):
    import ai_agents

    provider = SequenceProvider([{"content": "No debe ejecutarse.", "tool_call": None}])
    _install_provider(monkeypatch, provider)
    monkeypatch.setattr(ai_agents, "_public_vendor_rate_limit", lambda *args, **kwargs: False)
    client = _public_client(qa_public_vendor_db)

    response = _post_message(client, qa_public_vendor_db, "operation-rate-001", "Hola.")

    assert response.status_code == 429
    assert response.is_json
    assert provider.calls == 0


def test_duplicate_add_request_applies_quantity_and_rate_once(qa_public_vendor_db, monkeypatch):
    from services.ai_agent.vendor_order_service import CART_KEY
    from stockarmobile.models.conversations import Conversation

    rate_calls = []
    monkeypatch.setattr(
        "services.ai_agent.vendor_publication._write_rate_limit",
        lambda *args, **kwargs: rate_calls.append((args, kwargs)) or True,
    )
    client = _public_client(qa_public_vendor_db)
    payload = {
        "action": "add",
        "product_id": qa_public_vendor_db["product"].id,
        "quantity": 4,
    }
    url = f"/vendedor/{qa_public_vendor_db['slug']}/cart"
    headers = {"Idempotency-Key": "operation-add-4-machimbre"}
    first = client.post(url, headers=headers, json=payload)
    replay = client.post(
        url,
        headers=headers,
        json={**payload, "conversation_id": first.json["conversation_id"]},
    )
    conversation = Conversation.query.filter_by(
        company_id=qa_public_vendor_db["company"].id,
        id=first.json["conversation_id"],
    ).one()

    assert first.status_code == replay.status_code == 200
    assert replay.json["cart"]["items"][0]["quantity"] == 4
    assert conversation.metadata_json[CART_KEY][str(qa_public_vendor_db["product"].id)] == 4
    assert len(rate_calls) == 1


def test_vendor_tool_failure_returns_json_answer_not_http_500(qa_public_vendor_db, monkeypatch):
    from services.ai_agent.orchestrator_v2 import AgentRuntime

    provider = SequenceProvider([
        {
            "content": "",
            "tool_call": {
                "id": "tool-fails-001",
                "name": "buscar_producto",
                "arguments": {"query": "machimbre"},
            },
        },
        {"content": "No pude completar la búsqueda ahora.", "tool_call": None},
    ])
    _install_provider(monkeypatch, provider)

    def fail_tool(cls, *args, **kwargs):
        raise RuntimeError("simulated tool failure")

    monkeypatch.setattr(AgentRuntime, "_execute_tool", classmethod(fail_tool))
    _count_rate_limit(monkeypatch)
    client = _public_client(qa_public_vendor_db)
    response = _post_message(client, qa_public_vendor_db, "operation-tool-failure-001", "Buscá machimbre.")

    assert response.status_code == 200
    assert response.is_json
    assert response.json["content"] == "No pude completar la búsqueda ahora."
    assert provider.calls == 2


def test_direct_order_from_empty_cart_is_pending_and_replayed_once(qa_public_vendor_db, monkeypatch):
    from app import Payment, Quote, QuoteDelivery, Sale, db
    from stockarmobile.models.conversations import Conversation, ConversationMessage

    provider = SequenceProvider([
        {
            "content": "",
            "tool_call": {
                "id": "tool-order-1",
                "name": "preparar_pedido",
                "arguments": {
                    "product_query": "machimbre",
                    "quantity": 4,
                    "customer_name": "Waldo Ricollini",
                    "customer_phone": "3655344393",
                    "delivery_method": "envio",
                    "delivery_address": "Nueva Orleans 2332",
                    "delivery_city": "Resistencia",
                    "delivery_province": "Chaco",
                },
            },
        },
        {"content": "Pedido preparado.", "tool_call": None},
    ])
    _install_provider(monkeypatch, provider)
    rate_calls = _count_rate_limit(monkeypatch)
    mp_calls = []

    def ensure_token(self, *, company_id):
        return "qa-token"

    def create_preference(self, **kwargs):
        mp_calls.append(kwargs)
        return {"id": "pref-direct-001", "init_point": "https://payments.test/direct-001"}

    monkeypatch.setattr("services.ai_agent.vendor_order_service.MercadoPagoOAuthService.ensure_access_token", ensure_token)
    monkeypatch.setattr("services.ai_agent.vendor_order_service.MercadoPagoService.create_ai_order_checkout_preference", create_preference)
    client = _public_client(qa_public_vendor_db)
    message = "hola pasame un presupuesto de 4 metros de machimbre con envio a nueva orleans 2332 resistencia chaco. a nombre de waldo ricollini. cel 3655344393 y te quiero pagar"

    first = _post_message(client, qa_public_vendor_db, "operation-direct-order-001", message)
    second = _post_message(
        client,
        qa_public_vendor_db,
        "operation-direct-order-001",
        message,
        conversation_id=first.json["conversation_id"],
    )

    quote = Quote.query.filter_by(company_id=qa_public_vendor_db["company"].id).one()
    payment = Payment.query.filter_by(company_id=qa_public_vendor_db["company"].id, provider="mercadopago_ai_order").one()
    delivery = QuoteDelivery.query.filter_by(quote_id=quote.id).one()
    assert first.status_code == second.status_code == 200
    assert first.json == second.json
    assert "Waldo Ricollini" in first.json["content"]
    assert "4 × Machimbre pino" in first.json["content"]
    assert "Pagar con Mercado Pago" in first.json["content"]
    assert "Ver presupuesto" in first.json["content"]
    assert "pendiente de pago" in first.json["content"]
    assert provider.calls == 2
    assert len(rate_calls) == len(mp_calls) == 1
    assert Conversation.query.filter_by(company_id=qa_public_vendor_db["company"].id, channel="webchat").count() == 1
    assert ConversationMessage.query.filter_by(company_id=qa_public_vendor_db["company"].id, role="user").count() == 1
    assert ConversationMessage.query.filter_by(company_id=qa_public_vendor_db["company"].id, role="assistant").count() == 1
    assert len(quote.items) == 1
    assert quote.items[0].description == "Machimbre pino"
    assert float(quote.items[0].quantity) == 4
    assert quote.status == "ENVIADO"
    assert payment.status == "pending"
    assert payment.preference_id == "pref-direct-001"
    assert delivery.recipient_name == "Waldo Ricollini"
    assert delivery.phone == "3655344393"
    assert delivery.address == "Nueva Orleans 2332"
    assert delivery.city == "Resistencia"
    assert delivery.province == "Chaco"
    assert float(qa_public_vendor_db["product"].stock) == 20
    assert Sale.query.filter_by(company_id=qa_public_vendor_db["company"].id).count() == 0


def test_checkout_timeout_reuses_quote_and_same_mp_idempotency_reference(qa_public_vendor_setup, monkeypatch):
    from app import Payment, Quote

    client = _public_client(qa_public_vendor_setup)
    add_response = client.post(
        f"/vendedor/{qa_public_vendor_setup['slug']}/cart",
        headers={"Idempotency-Key": "operation-cart-for-checkout"},
        json={
            "conversation_id": None,
            "action": "add",
            "product_id": qa_public_vendor_setup["product"].id,
            "quantity": 4,
        },
    )
    assert add_response.status_code == 200
    mp_references = []
    attempt = {"count": 0}

    def ensure_token(self, *, company_id):
        return "qa-token"

    def create_preference(self, **kwargs):
        mp_references.append(kwargs["external_reference"])
        attempt["count"] += 1
        if attempt["count"] == 1:
            raise RuntimeError("simulated MP timeout")
        return {"id": "pref-checkout-retry", "init_point": "https://payments.test/retry"}

    monkeypatch.setattr("services.ai_agent.vendor_order_service.MercadoPagoOAuthService.ensure_access_token", ensure_token)
    monkeypatch.setattr("services.ai_agent.vendor_order_service.MercadoPagoService.create_ai_order_checkout_preference", create_preference)
    payload = {
        "conversation_id": add_response.json["conversation_id"],
        "customer_name": "Waldo Ricollini",
        "customer_phone": "3655344393",
        "delivery_method": "envio",
        "delivery_address": "Nueva Orleans 2332",
        "delivery_city": "Resistencia",
        "delivery_province": "Chaco",
    }
    url = f"/vendedor/{qa_public_vendor_setup['slug']}/checkout"
    first = client.post(url, headers={"Idempotency-Key": "operation-checkout-retry"}, json=payload)
    second = client.post(url, headers={"Idempotency-Key": "operation-checkout-retry"}, json=payload)

    assert first.status_code == 503 and first.is_json
    assert second.status_code == 200 and second.json["payment_url"] == "https://payments.test/retry"
    assert len(set(mp_references)) == 1
    assert len(mp_references) == 2
    assert Quote.query.filter_by(company_id=qa_public_vendor_setup["company"].id).count() == 1
    payment = Payment.query.filter_by(company_id=qa_public_vendor_setup["company"].id, provider="mercadopago_ai_order").one()
    assert payment.status == "pending"
    assert payment.preference_id == "pref-checkout-retry"


def test_other_company_cannot_reuse_conversation_or_operation_key(qa_public_vendor_setup, monkeypatch):
    from app import Company, db
    from services.ai_agent.config_service import ensure_default_agents
    from services.ai_agent.vendor_publication import publish_vendor

    company_b = Company(
        name="QA WebChat B",
        active=True,
        preferences_json=json.dumps({"ai_agent": {"enabled": True, "plan_code": "vendedor"}}),
    )
    db.session.add(company_b)
    db.session.flush()
    ensure_default_agents(company_b.id)
    publication_b = publish_vendor(company_b)
    db.session.commit()

    provider = SequenceProvider([{"content": "Respuesta A.", "tool_call": None}])
    _install_provider(monkeypatch, provider)
    client = _public_client(qa_public_vendor_setup)
    first = _post_message(client, qa_public_vendor_setup, "shared-operation-key", "Hola.")
    response_b = client.post(
        f"/vendedor/{publication_b['slug']}/message",
        headers={"Idempotency-Key": "shared-operation-key"},
        json={"message": "Hola.", "conversation_id": first.json["conversation_id"]},
    )

    assert first.status_code == 200
    assert response_b.status_code == 403
    assert response_b.is_json
    assert provider.calls == 1


def test_webchat_frontend_guards_click_enter_and_manual_retries():
    source = Path("templates/ai_agents/public_vendor_chat.html").read_text(encoding="utf-8")
    assert "let sendInFlight = false;" in source
    assert "if (!input || !send || sendInFlight) return;" in source
    assert "'Idempotency-Key': operation.key" in source
    assert "activeChatOperation = readStoredOperation(chatStorageKey);" in source
    assert "event.key === 'Enter' && !event.shiftKey" in source
    assert "send.textContent = activeChatOperation ? 'Reintentar' : 'Enviar';" in source
    assert "assistant_message_id" in source
    assert "function appendInlineMarkdown(parent, value)" in source
    assert "function safeMessageUrl(value)" in source
    assert "const heading = trimmed.match(/^#{1,6}\\s+(.+)$/)" in source
    assert "const bullet = trimmed.match(/^(?:\\*|-)\\s+(.+)$/)" in source
