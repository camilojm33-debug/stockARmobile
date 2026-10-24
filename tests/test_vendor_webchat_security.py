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


def test_public_chat_session_resolves_when_visitor_cookie_changes(qa_ai_database, monkeypatch):
    """The per-tab token must recover the same chat even if the Flask cookie changes."""
    import hashlib

    from services.ai_agent import vendor_publication
    from services.ai_agent.config_service import VENDOR_AGENT_NAME
    from stockarmobile.models.conversations import Agent, Conversation

    company = qa_ai_database["companies"]["vendedor"]
    agent = Agent.query.filter_by(company_id=company.id, name=VENDOR_AGENT_NAME).first()
    token = "qa-public-chat-session-" + ("x" * 32)
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

    monkeypatch.setattr(vendor_publication, "_visitor_id", lambda company_id: "qa-renewed-visitor")
    resolved_by_id = vendor_publication._public_conversation(
        company,
        conversation.id,
        create=False,
        chat_session_id=token,
    )
    resolved_after_reload = vendor_publication._public_conversation(
        company,
        create=False,
        chat_session_id=token,
    )

    assert resolved_by_id is not None
    assert resolved_by_id.id == conversation.id
    assert resolved_after_reload is not None
    assert resolved_after_reload.id == conversation.id


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
