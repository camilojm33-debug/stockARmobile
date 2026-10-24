import app as stock_app

from ai_agents import _decode_public_vendor_token, _public_vendor_token


def test_public_vendor_token_is_signed_and_company_scoped():
    with stock_app.app.app_context():
        token = _public_vendor_token(12345)
        assert _decode_public_vendor_token(token) == 12345
        assert _decode_public_vendor_token(token + "tampered") is None
        other = _public_vendor_token(54321)
        assert _decode_public_vendor_token(other) == 54321
        assert _decode_public_vendor_token(other) != 12345

def test_runtime_idempotent_retry_returns_original_assistant_content(qa_ai_database):
    from stockarmobile.models.conversations import Agent, Conversation, ConversationMessage

    company = qa_ai_database["companies"]["vendedor"]
    agent = Agent.query.filter_by(company_id=company.id).first()
    conversation = Conversation(company_id=company.id, agent_id=agent.id, channel="qa", status="open")
    db = stock_app.db
    db.session.add(conversation)
    db.session.flush()
    incoming = ConversationMessage(conversation_id=conversation.id, company_id=company.id, sender_type="user", role="user", content="hola", content_type="text", idempotency_key="same-key", trace_id="trace-1")
    assistant = ConversationMessage(conversation_id=conversation.id, company_id=company.id, sender_type="agent", role="assistant", content="respuesta original", content_type="text", trace_id="trace-1")
    db.session.add_all([incoming, assistant])
    db.session.flush()
    from services.ai_agent.orchestrator_v2 import AgentRuntime
    class Provider:
        def generate(self, **payload):
            raise AssertionError("no debería invocarse el proveedor en un retry idempotente")
    result = AgentRuntime.process(company_id=company.id, conversation_id=conversation.id, message="hola", channel="qa", idempotency_key="same-key", provider_override=Provider())
    assert result["status"] == "duplicate"
    assert result["content"] == "respuesta original"


def test_public_vendor_limits_fallback_without_redis_in_production(qa_ai_database, monkeypatch):
    from ai_agents import _public_vendor_rate_limit
    from services.ai_agent.vendor_publication import _write_rate_limit
    from stockarmobile.models.conversations import Agent, Conversation

    company = qa_ai_database["companies"]["vendedor"]
    agent = Agent.query.filter_by(company_id=company.id).first()
    conversation = Conversation(
        company_id=company.id,
        agent_id=agent.id,
        channel="webchat",
        external_conversation_id="qa-rate-visitor",
        status="open",
    )
    db = stock_app.db
    db.session.add(conversation)
    db.session.flush()

    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setitem(stock_app.app.config, "IS_PRODUCTION_ENV", True)
    with stock_app.app.test_request_context("/"):
        assert _public_vendor_rate_limit(
            company.id,
            conversation_id=conversation.id,
            request_key="qa-rate-message",
        ) is True
        assert _write_rate_limit(
            company.id,
            "cart",
            limit=30,
            conversation_id=conversation.id,
            request_key="qa-rate-cart",
        ) is True


def test_public_visitor_cookie_keeps_identity_when_flask_session_changes(qa_ai_database):
    from flask import g, jsonify, session

    from services.ai_agent import vendor_publication

    company_id = qa_ai_database["companies"]["vendedor"].id
    cookie_name = vendor_publication._public_visitor_cookie_name(company_id)
    session_key = f"public_vendor_visitor_{company_id}"

    with stock_app.app.test_request_context("/"):
        first = vendor_publication._visitor_id(company_id)
        response = vendor_publication._persist_public_visitor_cookies(jsonify({"success": True}))
        cookie_pair = response.headers["Set-Cookie"].split(";", 1)[0]
        cookie_value = cookie_pair.split("=", 1)[1]
        assert g._public_vendor_visitor_cookies[cookie_name] == cookie_value
        assert vendor_publication._public_visitor_cookie_serializer().loads(cookie_value) == first
        assert session[session_key] == first

    # The database fixture keeps an outer app context alive across requests;
    # clear its app-scoped g to model the clean context of a real HTTP request.
    g.pop("_public_vendor_visitor_cookies", None)

    # A new Flask session (as can happen after a session cookie is refreshed)
    # must continue to use the stable public-visitor cookie value.
    with stock_app.app.test_request_context("/", headers={"Cookie": f"{cookie_name}={cookie_value}"}):
        second = vendor_publication._visitor_id(company_id)
        assert second == first
        assert session[session_key] == first
        assert not getattr(g, "_public_vendor_visitor_cookies", {})


def test_public_visitor_cookie_rejects_a_forged_visitor_id(qa_ai_database):
    from flask import jsonify

    from services.ai_agent import vendor_publication

    company_id = qa_ai_database["companies"]["vendedor"].id
    cookie_name = vendor_publication._public_visitor_cookie_name(company_id)
    serializer = vendor_publication._public_visitor_cookie_serializer()
    victim_id = "a" * 32
    signed = serializer.dumps(victim_id)
    payload, signature = signed.rsplit(".", 1)
    tampered_signature = ("A" if signature[0] != "A" else "B") + signature[1:]
    tampered_cookie = payload + "." + tampered_signature

    with stock_app.app.test_request_context("/", headers={"Cookie": f"{cookie_name}={tampered_cookie}"}):
        visitor_id = vendor_publication._visitor_id(company_id)
        assert visitor_id != victim_id
        response = vendor_publication._persist_public_visitor_cookies(jsonify({"success": True}))
        replacement = response.headers["Set-Cookie"].split(";", 1)[0].split("=", 1)[1]
        assert serializer.loads(replacement) == visitor_id

def test_public_chat_session_cannot_resolve_with_another_tab_token(qa_ai_database, monkeypatch):
    import hashlib

    from services.ai_agent import vendor_publication
    from services.ai_agent.config_service import VENDOR_AGENT_NAME
    from stockarmobile.models.conversations import Agent, Conversation

    company = qa_ai_database["companies"]["vendedor"]
    agent = Agent.query.filter_by(company_id=company.id, name=VENDOR_AGENT_NAME).first()
    token = "qa-public-chat-session-" + ("a" * 32)
    other_token = "qa-public-chat-session-" + ("b" * 32)
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    conversation = Conversation(
        company_id=company.id,
        agent_id=agent.id,
        channel="webchat",
        external_conversation_id="qa-visitor",
        status="open",
        metadata_json={"source": "public_webchat_stable", "public_chat_session_digest": digest},
    )
    stock_app.db.session.add(conversation)
    stock_app.db.session.flush()
    monkeypatch.setattr(vendor_publication, "_visitor_id", lambda company_id: "qa-visitor")

    assert vendor_publication._public_conversation(
        company,
        conversation.id,
        create=False,
        chat_session_id=other_token,
    ) is None



def test_public_state_returns_persisted_chat_for_bound_visitor(qa_ai_database, monkeypatch):
    """The state endpoint must not turn a persisted chat into an empty state."""
    import hashlib

    from services.ai_agent import vendor_publication
    from services.ai_agent.config_service import VENDOR_AGENT_NAME
    from services.ai_agent.vendor_order_service import VendorOrderService
    from stockarmobile.models.conversations import Agent, Conversation, ConversationMessage

    company = qa_ai_database["companies"]["vendedor"]
    agent = Agent.query.filter_by(company_id=company.id, name=VENDOR_AGENT_NAME).first()
    token = "qa-public-chat-session-" + ("z" * 32)
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    conversation = Conversation(
        company_id=company.id,
        agent_id=agent.id,
        channel="webchat",
        external_conversation_id="qa-original-visitor",
        status="open",
        metadata_json={"source": "public_webchat_stable", "public_chat_session_digest": digest},
    )
    stock_app.db.session.add(conversation)
    stock_app.db.session.flush()
    stock_app.db.session.add_all([
        ConversationMessage(
            company_id=company.id,
            conversation_id=conversation.id,
            sender_type="user",
            role="user",
            content="Agregar Yerba Mate Tradicional 1 kg",
            content_type="text",
        ),
        ConversationMessage(
            company_id=company.id,
            conversation_id=conversation.id,
            sender_type="agent",
            role="assistant",
            content="Agregué 2 unidades al carrito.",
            content_type="text",
        ),
    ])
    stock_app.db.session.flush()

    expected_cart = {
        "items": [{
            "product_id": 71,
            "name": "Yerba Mate Tradicional 1 kg",
            "quantity": 2,
            "unit_measure": "u",
            "unit_price": 5355,
            "subtotal": 10710,
            "stock": 10,
        }],
        "total": 10710,
        "currency": "ARS",
        "line_count": 1,
    }
    monkeypatch.setattr(vendor_publication, "_public_available_company", lambda slug: company)
    monkeypatch.setattr(vendor_publication, "_visitor_id", lambda company_id: "qa-original-visitor")
    monkeypatch.setattr(
        vendor_publication,
        "can_use_ai",
        lambda company, agent: type("Access", (), {"allowed": True, "reason": ""})(),
    )
    monkeypatch.setattr(VendorOrderService, "get_cart", lambda **kwargs: expected_cart)

    with stock_app.app.test_request_context(
        f"/vendedor/demo/state?conversation_id={conversation.id}&chat_session_id={token}"
    ):
        response = vendor_publication.public_vendor_state("demo")

    payload = response.get_json()
    assert payload["success"] is True
    assert payload["conversation_id"] == conversation.id
    assert payload["cart"]["items"][0]["quantity"] == 2
    assert payload["cart"]["total"] == 10710
    assert [message["role"] for message in payload["messages"]] == ["user", "assistant"]
    assert "Yerba Mate" in payload["messages"][0]["content"]
