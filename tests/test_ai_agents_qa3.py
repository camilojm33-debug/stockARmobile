import json
from datetime import timedelta

import pytest

import app as stock_app
from app import Company, User, db
from services.ai_agent.campaign_service import CampaignService
from services.ai_agent.orchestrator_v2 import AgentRuntime, _explicit_price_action_confirmation
from services.ai_agent.usage_service import AI_PLANS, can_use_ai, can_use_ai_feature, record_ai_usage, usage_snapshot
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


@pytest.mark.parametrize(
    "plan_code,pricing_allowed,invoice_allowed",
    [
        ("inicio", False, False),
        ("vendedor", False, False),
        ("negocio", True, False),
        ("pro", True, True),
    ],
)
def test_plan_feature_entitlements(qa_ai_database, plan_code, pricing_allowed, invoice_allowed):
    company = qa_ai_database["companies"][plan_code]
    assert can_use_ai_feature(company, "pricing_controller").allowed is pricing_allowed
    assert can_use_ai_feature(company, "pricing_rollback").allowed is (plan_code == "pro")
    assert can_use_ai_feature(company, "facturas").allowed is invoice_allowed
    assert can_use_ai_feature(company, "crm").allowed is (plan_code == "pro")


def test_plan_feature_entitlements_do_not_depend_on_monthly_usage(qa_ai_database):
    company = qa_ai_database["companies"]["negocio"]
    limit = next(plan["limit"] for plan in AI_PLANS if plan["code"] == "negocio")
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
        for index in range(limit)
    ]
    db.session.execute(ConversationMessage.__table__.insert(), rows)
    db.session.flush()
    assert can_use_ai(company, "asistente").allowed is False
    assert can_use_ai_feature(company, "pricing_controller").allowed is True


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


def test_pricing_tools_follow_plan_entitlements_for_assistant_and_analyst(qa_ai_database):
    expected = {
        "inicio": set(),
        "negocio": {
            "consultar_precios",
            "analizar_oportunidades_precios",
            "previsualizar_cambio_precios",
            "confirmar_cambio_precios",
        },
        "pro": {
            "consultar_precios",
            "analizar_oportunidades_precios",
            "previsualizar_cambio_precios",
            "confirmar_cambio_precios",
            "revertir_cambio_precios",
        },
    }
    pricing_tool_names = {
        "consultar_precios",
        "analizar_oportunidades_precios",
        "previsualizar_cambio_precios",
        "confirmar_cambio_precios",
        "revertir_cambio_precios",
    }
    for plan_code, expected_pricing_tools in expected.items():
        company = qa_ai_database["companies"][plan_code]
        for agent_key in ("asistente", "analista"):
            names = {
                item["function"]["name"]
                for item in AgentRuntime._tool_definitions(agent_key, company_id=company.id)
            }
            assert {name for name in names if name in pricing_tool_names} == expected_pricing_tools


def test_analyst_chat_exposes_global_price_controller_to_the_authenticated_admin(qa_ai_database):
    company = qa_ai_database["companies"]["pro"]
    user = qa_ai_database["users"]["pro"]
    conversation = _conversation(company.id, "analista")
    calls = []

    class Provider:
        def generate(self, *, messages, tools=None, **kwargs):
            calls.append({"messages": list(messages), "tools": tools})
            return {"content": "Puedo preparar una vista previa del cambio para que la revises.", "tool_call": None}

    result = AgentRuntime.process(
        company_id=company.id,
        conversation_id=conversation.id,
        message="Quiero revisar los precios del comercio",
        channel="web",
        sender_id=user.id,
        provider_override=Provider(),
        include_system_prompt=True,
    )

    tool_names = {item["function"]["name"] for item in calls[0]["tools"]}
    assert {
        "consultar_precios",
        "analizar_oportunidades_precios",
        "previsualizar_cambio_precios",
        "confirmar_cambio_precios",
    } <= tool_names
    assert "revertir_cambio_precios" in tool_names
    system = calls[0]["messages"][0]["content"]
    assert "Controlador Global de Precios" in system
    assert "confirmación explícita" in system
    assert "vista previa" in system.lower()
    assert "Puedo preparar" in result["content"]


def test_price_confirmation_guard_blocks_same_turn_and_requires_the_shown_batch(qa_ai_database, monkeypatch):
    company = qa_ai_database["companies"]["pro"]
    user = qa_ai_database["users"]["pro"]
    apply_calls = []

    def fake_apply_batch(*, company_id, user_id, batch_id):
        apply_calls.append((company_id, user_id, batch_id))
        return {"success": True, "batch_id": batch_id, "applied": 3}

    monkeypatch.setattr("services.ai_agent.tools.pricing_controller.apply_batch", fake_apply_batch)
    arguments = {"batch_id": 42}

    same_turn = AgentRuntime._execute_tool(
        "confirmar_cambio_precios",
        company_id=company.id,
        arguments=arguments,
        context={"actor_user_id": user.id, "user_message": "Subí los precios un 10%", "previous_assistant_message": ""},
    )
    assert same_turn["success"] is False
    assert same_turn["requires_human_confirmation"] is True
    assert apply_calls == []

    wrong_batch = AgentRuntime._execute_tool(
        "confirmar_cambio_precios",
        company_id=company.id,
        arguments=arguments,
        context={
            "actor_user_id": user.id,
            "user_message": "Sí, confirmo",
            "previous_assistant_message": "Vista previa de precios creada. ID del lote: 41. Ningún precio fue modificado.",
        },
    )
    assert wrong_batch["success"] is False
    assert apply_calls == []

    confirmed = AgentRuntime._execute_tool(
        "confirmar_cambio_precios",
        company_id=company.id,
        arguments=arguments,
        context={
            "actor_user_id": user.id,
            "user_message": "Sí, confirmo el lote 42",
            "previous_assistant_message": "Vista previa creada. ID del lote: 42. Ningún precio fue modificado.",
        },
    )
    assert confirmed == {"success": True, "batch_id": 42, "applied": 3}
    assert apply_calls == [(company.id, user.id, 42)]


@pytest.mark.parametrize(
    "message,action,expected",
    [
        ("Sí, confirmo el lote 42", "apply", True),
        ("Subí 10% y confirmo", "apply", False),
        ("No confirmo", "apply", False),
        ("Confirmo", "apply", False),
        ("Revertí el lote 42", "rollback", True),
        ("Sí, revertí el lote 42", "rollback", True),
        ("Sí", "rollback", False),
    ],
)
def test_price_confirmation_intent_is_explicit_and_scoped(message, action, expected):
    previous = "Vista previa de precios creada. ID del lote: 42. Ningún precio fue modificado."
    assert _explicit_price_action_confirmation(
        message,
        action=action,
        batch_id=42,
        previous_assistant_message=previous,
    ) is expected


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



def test_marketing_proposal_chat_uses_prompt_and_persists_draft(qa_ai_database):
    from app import Campaign, db

    company = qa_ai_database["companies"]["pro"]
    user = qa_ai_database["users"]["pro"]
    conversation = _conversation(company.id, "marketing")
    calls = []

    class Provider:
        def generate(self, *, messages, tools=None, **kwargs):
            calls.append({"messages": list(messages), "tools": tools, "kwargs": kwargs})
            if len(calls) == 1:
                return {
                    "content": "",
                    "tool_call": {
                        "id": "proposal-1",
                        "name": "preparar_campana",
                        "arguments": {"campaign_type": "general", "channel": "email"},
                    },
                }
            return {"content": "Te preparé una propuesta para tu negocio.", "tool_call": None}

    result = AgentRuntime.process(
        company_id=company.id,
        conversation_id=conversation.id,
        message="haceme una propuesta",
        channel="web",
        sender_id=user.id,
        provider_override=Provider(),
        include_system_prompt=True,
    )

    assert result["content"].startswith("Te preparé una propuesta")
    assert calls[0]["messages"][0]["role"] == "system"
    assert "marketing ia" in calls[0]["messages"][0]["content"].lower()
    assert "preparar_campana" in {item["function"]["name"] for item in calls[0]["tools"]}
    campaign = Campaign.query.filter_by(company_id=company.id).one()
    assert campaign.status == "BORRADOR"
    assert campaign.created_by_user_id == user.id
    assert "Campaña #" in result["content"]



def test_marketing_opportunities_tool_respects_keyword_only_scope(qa_ai_database):
    from services.ai_agent.tools.analyst_marketing import OportunidadesMarketingTool

    company = qa_ai_database["companies"]["pro"]
    result = OportunidadesMarketingTool(company_id=company.id).execute(days=90, limit=20)
    assert result["success"] is True
    assert result["data_quality"] == "real_db_aggregates"


def test_marketing_prompt_uses_tenant_business_name(qa_ai_database):
    company = qa_ai_database["companies"]["pro"]
    user = qa_ai_database["users"]["pro"]
    conversation = _conversation(company.id, "marketing")
    calls = []

    class Provider:
        def generate(self, *, messages, tools=None, **kwargs):
            calls.append(list(messages))
            return {"content": "Propuesta preparada.", "tool_call": None}

    result = AgentRuntime.process(
        company_id=company.id,
        conversation_id=conversation.id,
        message="haceme una propuesta",
        channel="web",
        sender_id=user.id,
        provider_override=Provider(),
        include_system_prompt=True,
    )

    assert result["content"].startswith("Propuesta preparada.")
    assert "Campaña #" in result["content"]
    system = calls[0][0]["content"]
    assert company.name in system
    assert f"El equipo de {company.name}" in system
    assert "No firmes ni presentes la comunicación como StockArmobile" in system


def test_marketing_output_replaces_platform_identity_with_tenant_name(qa_ai_database):
    company = qa_ai_database["companies"]["pro"]
    user = qa_ai_database["users"]["pro"]
    conversation = _conversation(company.id, "marketing")

    class Provider:
        def generate(self, *, messages, tools=None, **kwargs):
            return {
                "content": "En StockARmobile queremos acompañarte. Saludos, El equipo de StockArMobile.",
                "tool_call": None,
            }

    result = AgentRuntime.process(
        company_id=company.id,
        conversation_id=conversation.id,
        message="haceme una propuesta",
        channel="web",
        sender_id=user.id,
        provider_override=Provider(),
        include_system_prompt=True,
    )

    assert company.name in result["content"]
    assert "StockARmobile" not in result["content"]
    assert "StockArMobile" not in result["content"]
    assert f"El equipo de {company.name}" in result["content"]


def test_company_and_global_ai_pause_block_agent_access(qa_ai_database, monkeypatch):
    company = qa_ai_database["companies"]["pro"]
    company.preferences_json = json.dumps({"ai_agent": {"plan_code": "pro", "enabled": False}})
    assert can_use_ai(company, "asistente").allowed is False

    company.preferences_json = json.dumps({"ai_agent": {"plan_code": "pro", "enabled": True}})
    monkeypatch.setenv("AI_AGENT_ENABLED", "false")
    assert can_use_ai(company, "asistente").allowed is False


def test_crm_tool_requires_pro_and_user_crm_permission(qa_ai_database):
    from services.ai_agent.orchestrator_v2 import AgentRuntime

    companies = qa_ai_database["companies"]
    users = qa_ai_database["users"]
    basic_names = {
        item["function"]["name"]
        for item in AgentRuntime._tool_definitions(
            "asistente",
            company_id=companies["inicio"].id,
            user_id=users["inicio"].id,
        )
    }
    assert "oportunidades_crm" not in basic_names

    pro_company = companies["pro"]
    employee = User(
        username="qa_crm_employee",
        email="qa_crm_employee@qa.local",
        password_hash="not-used",
        role="user",
        active=True,
        company_id=pro_company.id,
        permissions_json=json.dumps(["ai_access"]),
    )
    db.session.add(employee)
    db.session.flush()

    employee_names = {
        item["function"]["name"]
        for item in AgentRuntime._tool_definitions(
            "asistente",
            company_id=pro_company.id,
            user_id=employee.id,
        )
    }
    assert "oportunidades_crm" not in employee_names

    employee.permissions_json = json.dumps(["ai_access", "crm"])
    permitted_names = {
        item["function"]["name"]
        for item in AgentRuntime._tool_definitions(
            "asistente",
            company_id=pro_company.id,
            user_id=employee.id,
        )
    }
    assert "oportunidades_crm" in permitted_names

    employee.permissions_json = json.dumps(["ai_access"])
    denied = AgentRuntime._execute_tool(
        "oportunidades_crm",
        company_id=pro_company.id,
        arguments={},
        context={"actor_user_id": employee.id},
    )
    assert denied["success"] is False


def test_runtime_resumes_idempotent_inbound_without_assistant(qa_ai_database):
    company = qa_ai_database["companies"]["vendedor"]
    agent = __import__("services.ai_agent.config_service", fromlist=["ensure_default_agents"]).ensure_default_agents(company.id)["Vendedor 24 hs"]
    conversation = Conversation(company_id=company.id, agent_id=agent.id, channel="whatsapp", status="open")
    db.session.add(conversation)
    db.session.flush()
    incoming = ConversationMessage(
        conversation_id=conversation.id,
        company_id=company.id,
        sender_type="user",
        role="user",
        content="Tienen cafe?",
        content_type="text",
        external_message_id="wamid.resume.001",
        idempotency_key="whatsapp:wamid.resume.001",
        trace_id="trace-resume-001",
        metadata_json={"channel": "whatsapp"},
    )
    db.session.add(incoming)
    db.session.flush()

    class Provider:
        def generate(self, **kwargs):
            return {"content": "Si, tenemos cafe.", "tool_call": None}

    result = AgentRuntime.process(
        company_id=company.id,
        conversation_id=conversation.id,
        message=incoming.content,
        channel="whatsapp",
        external_message_id=incoming.external_message_id,
        idempotency_key=incoming.idempotency_key,
        provider_override=Provider(),
    )
    assistant = ConversationMessage.query.filter_by(
        conversation_id=conversation.id,
        company_id=company.id,
        role="assistant",
        trace_id=incoming.trace_id,
    ).first()
    assert result["status"] == "completed"
    assert assistant is not None
    assert assistant.content == "Si, tenemos cafe."
    assert ConversationMessage.query.filter_by(id=incoming.id).count() == 1


def test_deterministic_whatsapp_turn_reuses_messages_on_retry(qa_ai_database):
    from whatsapp_agent import _persist_deterministic_turn

    company = qa_ai_database["companies"]["vendedor"]
    agent = __import__("services.ai_agent.config_service", fromlist=["ensure_default_agents"]).ensure_default_agents(company.id)["Vendedor 24 hs"]
    conversation = Conversation(company_id=company.id, agent_id=agent.id, channel="whatsapp", status="open")
    db.session.add(conversation)
    db.session.flush()

    first = _persist_deterministic_turn(
        conversation,
        external_id="wamid.command.001",
        text="catalogo",
        response="Catálogo disponible.",
    )
    second = _persist_deterministic_turn(
        conversation,
        external_id="wamid.command.001",
        text="catalogo",
        response="Catálogo disponible.",
    )

    assert second.id == first.id
    assert ConversationMessage.query.filter_by(
        company_id=company.id,
        conversation_id=conversation.id,
        external_message_id="wamid.command.001",
        role="user",
    ).count() == 1
    assert ConversationMessage.query.filter_by(
        company_id=company.id,
        conversation_id=conversation.id,
        role="assistant",
    ).count() == 1
