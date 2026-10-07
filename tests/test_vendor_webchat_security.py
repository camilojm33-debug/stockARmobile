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


def test_public_vendor_limits_use_database_fallback_without_redis_in_production(monkeypatch, qa_ai_database):
    from ai_agents import _public_vendor_rate_limit
    from services.ai_agent.vendor_publication import _write_rate_limit

    company = qa_ai_database["companies"]["vendedor"]
    from stockarmobile.models.conversations import Agent, Conversation
    agent = Agent.query.filter_by(company_id=company.id).first()
    conversation = Conversation(
        company_id=company.id,
        agent_id=agent.id if agent else None,
        channel="webchat",
        external_conversation_id="qa-visitor",
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
            request_key="qa-message-1",
        ) is True
        assert _write_rate_limit(
            company.id,
            "cart",
            limit=30,
            conversation_id=conversation.id,
            request_key="qa-cart-1",
        ) is True
