import json
from datetime import timedelta

import pytest

import app as stock_app
from app import Company, User, db
from services.ai_agent.campaign_service import CampaignService
from services.ai_agent.orchestrator_v2 import AgentRuntime
from services.ai_agent.usage_service import AI_PLANS, can_use_ai, record_ai_usage, usage_snapshot
from stockarmobile.helpers.dates import utcnow_naive
from stockarmobile.models.conversations import Agent, Conversation, ConversationMessage


PLAN_AGENTS = {
    "inicio": {"asistente"},
    "vendedor": {"asistente", "vendedor"},
    "negocio": {"asistente", "vendedor", "analista"},
    "pro": {"asistente", "vendedor", "analista", "marketing"},
}


@pytest.fixture
def qa_ai_database():
    stock_app.app.config["TESTING"] = True
    stock_app.app.config["WTF_CSRF_ENABLED"] = False

    with stock_app.app.app_context():
        db.drop_all()
        db.create_all()
        preferences = lambda code: json.dumps({"ai_agent": {"plan_code": code}})
        companies = {
            code: Company(name=f"QA {code}", active=True, preferences_json=preferences(code))
            for code in PLAN_AGENTS
        }
        db.session.add_all(companies.values())
        db.session.flush()
        users = {}
        for code, company in companies.items():
            user = User(username=f"qa_{code}", email=f"{code}@qa.local", password_hash="not-used", role="admin", active=True, company_id=company.id)
            db.session.add(user)
            users[code] = user
        db.session.commit()
        yield {"companies": companies, "users": users}
        db.session.remove()
        db.drop_all()


def _conversation(company_id, agent_key=None):
    agent = None
    if agent_key in {"analista", "marketing"}:
        from services.ai_agent.config_service import ensure_agent_for_key

        agent = ensure_agent_for_key(company_id, agent_key)
    elif agent_key == "asistente":
        from services.ai_agent.config_service import ensure_default_agents

        agent = ensure_default_agents(company_id)["Asistente empresarial"]
    elif agent_key == "vendedor":
        from services.ai_agent.config_service import ensure_default_agents

        agent = ensure_default_agents(company_id)["Vendedor 24 hs"]
    conversation = Conversation(company_id=company_id, agent_id=agent.id if agent else None, channel="qa", status="open")
    db.session.add(conversation)
    db.session.flush()
    return conversation


@pytest.mark.parametrize("plan_code", [plan["code"] for plan in AI_PLANS])
def test_plan_matrix_is_enforced_by_can_use_ai(qa_ai_database, plan_code):
    company = qa_ai_database["companies"][plan_code]
    for agent_key in PLAN_AGENTS:
        access = can_use_ai(company, agent_key)
        assert access.allowed is (agent_key in PLAN_AGENTS[plan_code])


@pytest.mark.parametrize("plan_code", [plan["code"] for plan in AI_PLANS])
def test_limit_minus_one_then_limit_blocks_new_ai_use(qa_ai_database, plan_code):
    company = qa_ai_database["companies"][plan_code]
    limit = next(plan["limit"] for plan in AI_PLANS if plan["code"] == plan_code)
    conversation = _conversation(company.id, "asistente")
    now = utcnow_naive()
    rows = [
        {
            "conversation_id": conversation.id,
            "company_id": company.id,
            "sender_type": "agent",
            "role": "assistant",
            "content": f"respuesta {index}",
            "content_type": "text",
            "metadata_json": {"ai_usage_recorded": True, "ai_usage_period": now.strftime("%Y-%m"), "agent_key": "asistente"},
            "created_at": now,
        }
        for index in range(max(limit - 1, 0))
    ]
    if rows:
        db.session.execute(ConversationMessage.__table__.insert(), rows)
        db.session.flush()
    assert can_use_ai(company, "asistente").allowed is True

    final_message = ConversationMessage(
        conversation_id=conversation.id,
        company_id=company.id,
        sender_type="agent",
        role="assistant",
        content="respuesta en el límite",
        content_type="text",
        metadata_json={"agent_key": "asistente"},
        created_at=now,
    )
    db.session.add(final_message)
    db.session.flush()
    assert record_ai_usage(
        company_id=company.id,
        agent_id=conversation.agent_id,
        conversation_id=conversation.id,
        user_id=None,
        interaction_type="asistente",
        message_id=final_message.id,
    ) is True
    db.session.flush()
    assert usage_snapshot(company.id)["used_usage"] == limit
    assert can_use_ai(company, "asistente").allowed is False



def test_usage_period_isolated_by_month(qa_ai_database):
    company = qa_ai_database["companies"]["inicio"]
    conversation = _conversation(company.id, "asistente")
    now = utcnow_naive()
    previous = now.replace(day=1) - timedelta(days=1)
    old_message = ConversationMessage(
        conversation_id=conversation.id,
        company_id=company.id,
        sender_type="agent",
        role="assistant",
        content="respuesta anterior",
        content_type="text",
        metadata_json={"ai_usage_recorded": True, "ai_usage_period": previous.strftime("%Y-%m"), "agent_key": "asistente"},
        created_at=previous,
    )
    current_message = ConversationMessage(
        conversation_id=conversation.id,
        company_id=company.id,
        sender_type="agent",
        role="assistant",
        content="respuesta actual",
        content_type="text",
        metadata_json={"agent_key": "asistente"},
        created_at=now,
    )
    db.session.add_all([old_message, current_message])
    db.session.flush()
    assert record_ai_usage(company_id=company.id, agent_id=conversation.agent_id, conversation_id=conversation.id, user_id=None, interaction_type="asistente", message_id=current_message.id) is True
    db.session.flush()
    snapshot = usage_snapshot(company.id, now=now)
    assert snapshot["period"] == now.strftime("%Y-%m")
    assert snapshot["used_usage"] == 1


def test_usage_recording_is_idempotent_and_distinct_messages_count_once(qa_ai_database):
    company = qa_ai_database["companies"]["inicio"]
    conversation = _conversation(company.id, "asistente")
    messages = [
        ConversationMessage(conversation_id=conversation.id, company_id=company.id, sender_type="agent", role="assistant", content=f"respuesta {index}", content_type="text", metadata_json={"agent_key": "asistente"})
        for index in range(2)
    ]
    db.session.add_all(messages)
    db.session.flush()
    arguments = {"company_id": company.id, "agent_id": conversation.agent_id, "conversation_id": conversation.id, "user_id": None, "interaction_type": "asistente"}
    assert record_ai_usage(**arguments, message_id=messages[0].id) is True
    assert record_ai_usage(**arguments, message_id=messages[0].id) is False
    assert record_ai_usage(**arguments, message_id=messages[1].id) is True
    db.session.flush()
    assert usage_snapshot(company.id)["used_usage"] == 2


@pytest.mark.parametrize("plan_code", ["inicio", "vendedor", "negocio"])
def test_backend_blocks_unincluded_agent_before_provider_and_without_campaign(qa_ai_database, plan_code):
    company = qa_ai_database["companies"][plan_code]
    conversation = _conversation(company.id, "marketing")
    provider_calls = []

    class Provider:
        def generate(self, **payload):
            provider_calls.append(payload)
            return {"content": "no debería ejecutarse"}

    with pytest.raises(ValueError, match="plan IA|requiere"):
        AgentRuntime.process(company_id=company.id, conversation_id=conversation.id, message="Crear campaña", channel="web", provider_override=Provider())
    assert provider_calls == []
    assert usage_snapshot(company.id)["used_usage"] == 0
    from app import Campaign

    assert Campaign.query.filter_by(company_id=company.id).count() == 0


def test_agent_selection_restricts_tools_and_preserves_isolation(qa_ai_database):
    company = qa_ai_database["companies"]["pro"]
    expected_tools = {
        "asistente": {"buscar_producto", "resumen_ventas", "stock_critico"},
        "vendedor": {"buscar_producto", "preparar_pedido"},
        "analista": {"comparar_ventas", "productos_mas_vendidos", "stock_critico"},
        "marketing": {"preparar_campana", "productos_promocionables", "clientes_inactivos"},
    }
    for agent_key in expected_tools:
        conversation = _conversation(company.id, agent_key)
        payloads = []

        class Provider:
            def generate(self, **payload):
                payloads.append(payload)
                return {"content": "respuesta válida"}

        result = AgentRuntime.process(company_id=company.id, conversation_id=conversation.id, message=f"Consulta {agent_key}", channel="web", provider_override=Provider())
        assert result["agent_id"] == conversation.agent_id
        available = {tool["function"]["name"] for tool in payloads[0]["tools"]}
        assert expected_tools[agent_key] <= available
        if agent_key == "analista":
            assert "preparar_pedido" not in available
        if agent_key == "marketing":
            assert "comparar_ventas" not in available


def test_provider_error_does_not_record_usage(qa_ai_database):
    company = qa_ai_database["companies"]["inicio"]
    conversation = _conversation(company.id, "asistente")

    class FailingProvider:
        def generate(self, **payload):
            raise RuntimeError("provider unavailable")

    with pytest.raises(RuntimeError, match="provider unavailable"):
        AgentRuntime.process(company_id=company.id, conversation_id=conversation.id, message="Consulta", channel="web", provider_override=FailingProvider())
    db.session.rollback()
    assert usage_snapshot(company.id)["used_usage"] == 0
    assert ConversationMessage.query.filter_by(company_id=company.id, role="assistant").count() == 0


def test_campaigns_are_tenant_isolated(qa_ai_database):
    companies = qa_ai_database["companies"]
    campaign = CampaignService.create_draft(company_id=companies["pro"].id, user_id=qa_ai_database["users"]["pro"].id, title="Campaña A", objective="Promoción", campaign_type="general", content="Borrador", system_data={}, audience_segment="clientes activos", audience_count=1)
    db.session.commit()
    assert CampaignService._campaign(companies["pro"].id, campaign.id) is not None
    assert CampaignService._campaign(companies["inicio"].id, campaign.id) is None
