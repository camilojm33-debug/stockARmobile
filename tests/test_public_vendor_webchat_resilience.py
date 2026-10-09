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
        self.invocations = []

    def generate(self, **kwargs):
        self.calls += 1
        self.invocations.append({
            "messages": list(kwargs.get("messages") or []),
            "tools": kwargs.get("tools"),
            "kwargs": {key: value for key, value in kwargs.items() if key not in {"messages", "tools"}},
        })
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


def test_general_chat_turn_does_not_reexpose_previous_checkout_links(qa_public_vendor_db, monkeypatch):
    from services.ai_agent.providers.base import AIProviderError

    provider = SequenceProvider([
        {
            "content": "",
            "tool_call": {
                "id": "tool-order-link-gate",
                "name": "preparar_pedido",
                "arguments": {
                    "product_query": "machimbre",
                    "quantity": 2,
                    "customer_name": "Julia Acosta",
                    "customer_phone": "3624001122",
                    "delivery_method": "retiro",
                },
            },
        },
        {"content": "Pedido preparado.", "tool_call": None},
        {
            "content": "Tenemos otros productos disponibles. ¿Qué estás buscando?\\n\\n[Pagar con Mercado Pago](https://www.mercadopago.com.ar/checkout/v1/redirect?pref_id=old)\\n[Ver presupuesto](https://www.stockarmobile.com/presupuestos/publico/old-token)",
            "tool_call": None,
        },
    ])
    _install_provider(monkeypatch, provider)

    def ensure_token(self, *, company_id):
        return "qa-token"

    def create_preference(self, **kwargs):
        return {"id": "pref-stale-link", "init_point": "https://payments.test/stale-link"}

    monkeypatch.setattr("services.ai_agent.vendor_order_service.MercadoPagoOAuthService.ensure_access_token", ensure_token)
    monkeypatch.setattr("services.ai_agent.vendor_order_service.MercadoPagoService.create_ai_order_checkout_preference", create_preference)

    client = _public_client(qa_public_vendor_db)
    first = _post_message(
        client,
        qa_public_vendor_db,
        "operation-create-link-gate",
        "Necesito 2 metros de machimbre, a nombre de Julia Acosta, teléfono 3624001122, retiro en local.",
    )
    first_tool_messages = [
        item for item in provider.invocations[1]["messages"]
        if item.get("role") == "tool"
    ]
    assert len(first_tool_messages) == 1
    first_tool_result = json.loads(first_tool_messages[0]["content"])
    assert first_tool_result.get("success") is True, first_tool_result
    assert first_tool_result.get("status") != "needs_customer_details", first_tool_result

    second = _post_message(
        client,
        qa_public_vendor_db,
        "operation-general-after-checkout",
        "¿Qué otros productos tienen?",
        conversation_id=first.json["conversation_id"],
    )

    assert first.status_code == second.status_code == 200
    assert first.json["payment_url"] == "https://payments.test/stale-link"
    assert first.json["quote_url"]
    assert second.json["payment_url"] is None
    assert second.json["quote_url"] is None
    assert second.json["total"] is None
    assert "Julia Acosta" not in second.json["content"]
    assert "mercadopago.com.ar/checkout" not in second.json["content"]
    assert "stockarmobile.com/presupuestos/publico" not in second.json["content"]


def test_new_order_without_new_checkout_does_not_replay_previous_customer_link(qa_public_vendor_db, monkeypatch):
    provider = SequenceProvider([
        {
            "content": "",
            "tool_call": {
                "id": "tool-first-order",
                "name": "preparar_pedido",
                "arguments": {
                    "product_query": "machimbre",
                    "quantity": 2,
                    "customer_name": "Julia Acosta",
                    "customer_phone": "3624001122",
                    "delivery_method": "retiro",
                },
            },
        },
        {"content": "Pedido preparado.", "tool_call": None},
        {
            "content": "¡Listo Pedro! Nuevo presupuesto. [Pagar con Mercado Pago](https://www.mercadopago.com.ar/checkout/v1/redirect?pref_id=previous)",
            "tool_call": None,
        },
    ])
    _install_provider(monkeypatch, provider)

    def ensure_token(self, *, company_id):
        return "qa-token"

    def create_preference(self, **kwargs):
        return {"id": "pref-previous-customer", "init_point": "https://payments.test/previous-customer"}

    monkeypatch.setattr("services.ai_agent.vendor_order_service.MercadoPagoOAuthService.ensure_access_token", ensure_token)
    monkeypatch.setattr("services.ai_agent.vendor_order_service.MercadoPagoService.create_ai_order_checkout_preference", create_preference)

    client = _public_client(qa_public_vendor_db)
    first = _post_message(
        client,
        qa_public_vendor_db,
        "operation-first-order-safe",
        "Necesito 2 metros de machimbre, a nombre de Julia Acosta, teléfono 3624001122, retiro en local.",
    )
    second = _post_message(
        client,
        qa_public_vendor_db,
        "operation-new-order-without-tool",
        "Soy Pedro, necesito un nuevo presupuesto de 3 metros de machimbre, a nombre de Pedro Silva, teléfono 3624556789, retiro en local.",
        conversation_id=first.json["conversation_id"],
    )

    assert first.status_code == second.status_code == 200
    assert second.json["payment_url"] is None
    assert second.json["quote_url"] is None
    assert "No pude generar un presupuesto nuevo" in second.json["content"]
    assert "pref_id=previous" not in second.json["content"]
    assert "Julia Acosta" not in second.json["content"]


def test_new_quote_intent_does_not_capture_previous_quote_retrieval():
    from services.ai_agent.vendor_publication import (
        _assistant_requests_order_details,
        _starts_new_quote_request,
    )
    from types import SimpleNamespace
    from services.ai_agent.orchestrator_v2 import (
        _extract_public_customer_name,
        _same_public_customer_name,
    )

    assert _starts_new_quote_request("Necesito otro presupuesto") is True
    assert _starts_new_quote_request("Quiero cotizar otro producto") is True
    assert _starts_new_quote_request("Quiero otro presupuesto parecido al anterior") is True
    assert _starts_new_quote_request("Haceme un presupuesto con 3 metros de machimbre") is True
    assert _starts_new_quote_request("Quiero consultar cuánto era el presupuesto") is False
    assert _same_public_customer_name("Nelson Mandele", "Nelson Mandela") is True
    assert _same_public_customer_name("Julia Acosta", "María Rodríguez") is False
    assert _extract_public_customer_name([
        SimpleNamespace(content="hola soy nelson mandela"),
    ]) == "Nelson Mandela"
    assert _extract_public_customer_name([
        SimpleNamespace(content="Hola, soy Nelson Mandela y necesito un presupuesto"),
    ]) == "Nelson Mandela"
    assert _starts_new_quote_request("Mostrame el presupuesto anterior") is False
    assert _starts_new_quote_request("Pasame el link del presupuesto anterior") is False
    assert _starts_new_quote_request("Quiero consultar el estado del presupuesto") is False
    assert _assistant_requests_order_details("¿Qué producto y qué cantidad necesitás?") is True
    assert _assistant_requests_order_details(
        "¡Listo! Ya preparé tu presupuesto. ¿Qué producto necesitás?"
    ) is False


def test_public_webchat_prefers_older_explicit_name_over_model_typo(qa_public_vendor_db, monkeypatch):
    """A self-identification must survive long chats and override an LLM misspelling."""
    from app import db
    from services.ai_agent.orchestrator_v2 import VendorOrderPreviewTool, VendorOrderService
    from stockarmobile.models.conversations import Conversation, ConversationMessage

    setup = qa_public_vendor_db
    created = {}

    with setup["app"].app_context():
        conversation = Conversation(
            company_id=setup["company"].id,
            channel="webchat",
            external_conversation_id="qa-old-name-history",
            status="open",
            metadata_json={
                "customer_name": "Nelson Mandele",
                "customer_phone": "3624001122",
                "delivery": {
                    "method": "retiro",
                    "recipient_name": "Nelson Mandele",
                    "phone": "3624001122",
                },
            },
        )
        db.session.add(conversation)
        db.session.flush()

        # Place the authoritative introduction outside the ordinary 20-message
        # operating history, as happens when a customer chats before checkout.
        db.session.add(ConversationMessage(
            conversation_id=conversation.id,
            company_id=setup["company"].id,
            sender_type="customer",
            role="user",
            content="hola soy nelson mandela",
        ))
        for index in range(22):
            db.session.add(ConversationMessage(
                conversation_id=conversation.id,
                company_id=setup["company"].id,
                sender_type="customer",
                role="user",
                content=f"Consulta de seguimiento número {index}",
            ))
        db.session.flush()

        monkeypatch.setattr(
            VendorOrderService,
            "get_cart",
            staticmethod(lambda **kwargs: {
                "items": [], "total": 0, "currency": "ARS", "line_count": 0,
            }),
        )
        monkeypatch.setattr(
            VendorOrderService,
            "update_cart",
            staticmethod(lambda **kwargs: {"success": True, "items": []}),
        )

        def fake_create_pending_order(**kwargs):
            created.update(kwargs)
            return {"success": True, "quote_number": "P-000099", "total": 21600}

        monkeypatch.setattr(
            VendorOrderService,
            "create_pending_order",
            staticmethod(fake_create_pending_order),
        )

        tool = VendorOrderPreviewTool(
            company_id=setup["company"].id,
            conversation_id=conversation.id,
            channel="webchat",
        )
        result = tool.execute(
            product_query="machimbre",
            quantity=3,
            customer_name="nelson mandele",
            customer_phone="3624001122",
        )

        assert result["success"] is True
        assert created["customer_name"] == "Nelson Mandela"
        assert created["customer_phone"] == "3624001122"
        db.session.rollback()


def test_public_vendor_stock_outputs_normalize_float_noise(qa_public_vendor_db):
    from app import db
    from services.ai_agent.tools.product_search import BuscarProductoTool
    from services.ai_agent.tools.stock_query import ConsultarStockTool
    from services.ai_agent.vendor_order_service import VendorOrderService

    setup = qa_public_vendor_db
    with setup["app"].app_context():
        product = setup["product"]
        product.stock = 17.80000000000001
        db.session.flush()

        stock_result = ConsultarStockTool(company_id=setup["company"].id).execute(
            product_id=product.id,
        )
        search_result = BuscarProductoTool(company_id=setup["company"].id).execute(
            query="Machimbre",
        )
        catalog_result = VendorOrderService.list_catalog(
            company_id=setup["company"].id,
            query="Machimbre",
        )

        assert stock_result["product"]["stock"] == 17.8
        assert search_result["items"][0]["stock"] == 17.8
        assert catalog_result["products"][0]["stock"] == 17.8


def test_public_webchat_keeps_customer_quantity_when_model_misreads_stock(qa_public_vendor_db, monkeypatch):
    from app import db
    from services.ai_agent.orchestrator_v2 import VendorOrderPreviewTool, VendorOrderService
    from services.ai_agent.vendor_order_service import CART_KEY
    from stockarmobile.models.conversations import Conversation, ConversationMessage

    setup = qa_public_vendor_db
    observed = {}

    with setup["app"].app_context():
        product = setup["product"]
        product.stock = 17.80000000000001
        conversation = Conversation(
            company_id=setup["company"].id,
            channel="webchat",
            external_conversation_id="qa-stock-quantity-authority",
            status="open",
            metadata_json={
                CART_KEY: {str(product.id): 4},
                "customer_name": "Nelson Mandela",
                "customer_phone": "344344223",
                "delivery": {
                    "method": "envio",
                    "recipient_name": "Nelson Mandela",
                    "phone": "344344223",
                    "address": "calle siempreviva 435",
                    "city": "Resistencia",
                    "province": "Chaco",
                },
            },
        )
        db.session.add(conversation)
        db.session.flush()

        messages = [
            "hola soy nelson mandela",
            "cotizame 4 metros de machimbre con envio",
            "cel 344344223 a calle siempreviva 435 resistencia chaco",
            "Actualmente no contamos con stock suficiente; tenemos disponible 1,80 metros.",
            "si esta bien",
        ]
        for content in messages:
            role = "assistant" if content.startswith("Actualmente") else "user"
            db.session.add(ConversationMessage(
                conversation_id=conversation.id,
                company_id=setup["company"].id,
                sender_type="assistant" if role == "assistant" else "customer",
                role=role,
                content=content,
            ))
        db.session.flush()

        def fake_create_pending_order(**kwargs):
            cart = VendorOrderService.get_cart(
                company_id=setup["company"].id,
                conversation_id=conversation.id,
            )
            observed["quantity"] = cart["items"][0]["quantity"]
            observed["stock"] = cart["items"][0]["stock"]
            observed.update(kwargs)
            return {
                "success": True,
                "quote_number": "P-TEST",
                "total": cart["total"],
            }

        monkeypatch.setattr(
            VendorOrderService,
            "create_pending_order",
            staticmethod(fake_create_pending_order),
        )

        tool = VendorOrderPreviewTool(
            company_id=setup["company"].id,
            conversation_id=conversation.id,
            channel="webchat",
        )
        result = tool.execute(
            product_query="machimbre",
            quantity=1.8,
            customer_name="Nelson Mandele",
            customer_phone="344344223",
            delivery_method="envio",
            delivery_address="calle siempreviva 435",
            delivery_city="Resistencia",
            delivery_province="Chaco",
        )

        assert result["success"] is True
        assert observed["quantity"] == 4
        assert observed["stock"] == 17.8
        assert observed["customer_name"] == "Nelson Mandela"
        db.session.rollback()


def test_public_chat_can_start_second_quote_after_clarification(qa_public_vendor_db, monkeypatch):
    from app import Payment, Quote, QuoteDelivery
    from stockarmobile.models.conversations import Conversation

    provider = SequenceProvider([
        {
            "content": "",
            "tool_call": {
                "id": "tool-first-quote",
                "name": "preparar_pedido",
                "arguments": {
                    "product_query": "machimbre",
                    "quantity": 2,
                    "customer_name": "Julia Acosta",
                    "customer_phone": "3624001122",
                    "delivery_method": "retiro",
                },
            },
        },
        {"content": "Pedido preparado.", "tool_call": None},
        {
            "content": "¡Claro! ¿Qué producto y qué cantidad necesitás cotizar para preparar otro presupuesto?",
            "tool_call": None,
        },
        {
            "content": "",
            "tool_call": {
                "id": "tool-second-quote",
                "name": "preparar_pedido",
                "arguments": {
                    "product_query": "machimbre",
                    "quantity": 3,
                },
            },
        },
        {"content": "Presupuesto preparado.", "tool_call": None},
    ])
    _install_provider(monkeypatch, provider)
    preferences = []

    def ensure_token(self, *, company_id):
        return "qa-token"

    def create_preference(self, **kwargs):
        preferences.append(kwargs)
        number = len(preferences)
        return {
            "id": f"pref-second-quote-{number}",
            "init_point": f"https://payments.test/second-quote-{number}",
        }

    monkeypatch.setattr(
        "services.ai_agent.vendor_order_service.MercadoPagoOAuthService.ensure_access_token",
        ensure_token,
    )
    monkeypatch.setattr(
        "services.ai_agent.vendor_order_service.MercadoPagoService.create_ai_order_checkout_preference",
        create_preference,
    )

    client = _public_client(qa_public_vendor_db)
    conversation_setup = qa_public_vendor_db
    first = _post_message(
        client,
        conversation_setup,
        "operation-first-quote-same-client",
        "Necesito 2 metros de machimbre, a nombre de Julia Acosta, teléfono 3624001122, retiro en local.",
    )
    second = _post_message(
        client,
        conversation_setup,
        "operation-request-second-quote",
        "Necesito otro presupuesto",
        conversation_id=first.json["conversation_id"],
    )

    assert first.status_code == second.status_code == 200
    assert first.json["payment_url"] == "https://payments.test/second-quote-1"
    assert second.json["payment_url"] is None
    assert second.json["quote_url"] is None
    assert "¿Qué producto y qué cantidad" in second.json["content"]
    assert "No pude generar un presupuesto nuevo" not in second.json["content"]
    assert "second-quote-1" not in second.json["content"]
    assert second.json["cart"]["items"] == []

    conversation = Conversation.query.filter_by(
        id=first.json["conversation_id"],
        company_id=conversation_setup["company"].id,
    ).one()
    assert conversation.metadata_json["vendor_new_quote_context"] is True

    third = _post_message(
        client,
        conversation_setup,
        "operation-details-second-quote",
        "3 metros de machimbre",
        conversation_id=first.json["conversation_id"],
    )

    assert third.status_code == 200
    assert third.json["payment_url"] == "https://payments.test/second-quote-2"
    assert third.json["payment_url"] != first.json["payment_url"]
    assert third.json["quote_url"] != first.json["quote_url"]
    assert provider.calls == 5
    assert len(preferences) == 2
    assert preferences[0]["external_reference"] != preferences[1]["external_reference"]

    quotes = (
        Quote.query.filter_by(
            company_id=conversation_setup["company"].id,
            observations="Pedido generado por el Vendedor 24 hs de StockARmobile.",
        )
        .order_by(Quote.id.asc())
        .all()
    )
    assert len(quotes) == 2
    assert quotes[0].id != quotes[1].id
    assert quotes[0].number != quotes[1].number
    assert float(quotes[0].items[0].quantity) == 2
    assert float(quotes[1].items[0].quantity) == 3
    assert quotes[0].items[0].description == quotes[1].items[0].description
    first_delivery = QuoteDelivery.query.filter_by(quote_id=quotes[0].id).one()
    second_delivery = QuoteDelivery.query.filter_by(quote_id=quotes[1].id).one()
    assert second_delivery.recipient_name == first_delivery.recipient_name == "Julia Acosta"
    assert second_delivery.phone == first_delivery.phone == "3624001122"
    assert second_delivery.method == first_delivery.method == "retiro"

    payments = (
        Payment.query.filter_by(
            company_id=conversation_setup["company"].id,
            provider="mercadopago_ai_order",
        )
        .order_by(Payment.id.asc())
        .all()
    )
    assert len(payments) == 2
    assert [payment.preference_id for payment in payments] == [
        "pref-second-quote-1",
        "pref-second-quote-2",
    ]
    assert all(payment.status == "pending" for payment in payments)
    assert "vendor_new_quote_context" not in conversation.metadata_json


def test_public_chat_can_list_the_full_active_catalog(qa_public_vendor_db, monkeypatch):
    import json

    provider = SequenceProvider([
        {
            "content": "",
            "tool_call": {
                "id": "catalog-query-1",
                "name": "ver_catalogo",
                "arguments": {"query": "", "limit": 20},
            },
        },
        {"content": "El catálogo activo tiene Machimbre pino y clavos galvanizados; consultá precios y stock en la ficha de cada uno.", "tool_call": None},
    ])
    _install_provider(monkeypatch, provider)
    from app import Product, db
    extra_product = Product(
        barcode="CLV-002",
        name="Clavos galvanizados",
        price=3500,
        cost_price=2000,
        stock=15,
        min_stock=1,
        active=True,
        company_id=qa_public_vendor_db["company"].id,
        unit_measure="kg",
    )
    db.session.add(extra_product)
    db.session.flush()
    client = _public_client(qa_public_vendor_db)

    response = _post_message(
        client,
        qa_public_vendor_db,
        "operation-open-catalog",
        "¿Y qué otra cosa tenés?",
    )

    assert response.status_code == 200
    assert "Machimbre pino" in response.json["content"]
    offered_tools = {
        item["function"]["name"]
        for item in (provider.invocations[0].get("tools") or [])
    }
    assert "ver_catalogo" in offered_tools
    tool_messages = [m for m in provider.invocations[1]["messages"] if m.get("role") == "tool"]
    assert len(tool_messages) == 1
    catalog = json.loads(tool_messages[0]["content"])
    assert catalog["success"] is True
    assert catalog["count"] == 2
    products = {item["name"]: item for item in catalog["products"]}
    assert set(products) == {"Machimbre pino", "Clavos galvanizados"}
    assert products["Machimbre pino"]["price"] == 6600.0
    assert products["Clavos galvanizados"]["price"] == 3500.0
    assert products["Clavos galvanizados"]["stock"] == 15.0
    assert "no se obtuvieron resultados" not in response.json["content"].lower()


def test_public_quote_with_missing_contact_asks_clarification_without_failing(qa_public_vendor_db, monkeypatch):
    from app import Payment, Quote

    provider = SequenceProvider([
        {
            "content": "",
            "tool_call": {
                "id": "quote-missing-details",
                "name": "preparar_pedido",
                "arguments": {"product_query": "machimbre", "quantity": 3},
            },
        },
        {
            "content": "¡Dale! Ya tengo los 3 metros de machimbre en el carrito. ¿Me confirmás tu nombre, teléfono y si preferís retiro o envío?",
            "tool_call": None,
        },
    ])
    _install_provider(monkeypatch, provider)
    client = _public_client(qa_public_vendor_db)
    response = _post_message(
        client,
        qa_public_vendor_db,
        "operation-quote-missing-contact",
        "Haceme un presupuesto con 3 metros de machimbre",
    )

    assert response.status_code == 200
    assert "No pude generar un presupuesto nuevo" not in response.json["content"]
    assert "teléfono" in response.json["content"].lower()
    assert "retiro o envío" in response.json["content"].lower()
    assert response.json["payment_url"] is None
    assert response.json["quote_url"] is None
    assert len(response.json["cart"]["items"]) == 1
    assert response.json["cart"]["items"][0]["quantity"] == 3
    assert Quote.query.filter_by(
        company_id=qa_public_vendor_db["company"].id,
        observations="Pedido generado por el Vendedor 24 hs de StockARmobile.",
    ).count() == 0
    assert Payment.query.filter_by(
        company_id=qa_public_vendor_db["company"].id,
        provider="mercadopago_ai_order",
    ).count() == 0


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
