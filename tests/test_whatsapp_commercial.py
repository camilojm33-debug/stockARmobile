import json
from unittest.mock import patch

import pytest

import app as stock_app
from app import Company, SaaSLead, SaaSLeadConsent, User, db
from services.ai_agent.orchestrator_v2 import AgentRuntime
from services.ai_agent.usage_service import can_use_ai
from services.saas_commercial_whatsapp import (
    COMMERCIAL_AGENT_NAME,
    COMMERCIAL_COMPANY_MARKER,
    capture_inbound_lead,
    get_commercial_company,
    get_commercial_conversation,
    is_commercial_phone_number_id,
    process_commercial_message,
)
from stockarmobile.models.conversations import Agent, AgentConfiguration


@pytest.fixture
def commercial_database(monkeypatch):
    stock_app.app.config["TESTING"] = True
    stock_app.app.config["WTF_CSRF_ENABLED"] = False

    monkeypatch.setenv("WHATSAPP_COMMERCIAL_ENABLED", "1")
    monkeypatch.setenv("WHATSAPP_COMMERCIAL_PHONE_NUMBER_ID", "COMMERCIAL_PHONE_QA")
    monkeypatch.setenv("WHATSAPP_COMMERCIAL_WABA_ID", "COMMERCIAL_WABA_QA")
    monkeypatch.setenv("WHATSAPP_COMMERCIAL_ACCESS_TOKEN", "token-for-qa")

    with stock_app.app.app_context():
        db.drop_all()
        db.create_all()

        company = Company(
            name="StockArMobile Comercial",
            active=True,
            language="es",
            timezone="America/Argentina/Buenos_Aires",
            currency="ARS",
            date_format="%Y-%m-%d",
            numbering_format="es_AR",
            preferences_json=json.dumps({
                "internal_channel": COMMERCIAL_COMPANY_MARKER,
                "ai_agent": {
                    "enabled": True,
                    "plan_code": "pro",
                    "status": "ACTIVA",
                    "whatsapp": {"enabled": False, "phone_number_id": ""},
                },
            }),
        )
        db.session.add(company)
        actor = User(
            username="qa_superadmin",
            email="qa-superadmin@local",
            password_hash="unused",
            role="superadmin",
            active=True,
            company_id=None,
        )
        db.session.add(actor)
        db.session.flush()

        agent = Agent(
            company_id=company.id,
            name=COMMERCIAL_AGENT_NAME,
            description="QA commercial agent",
            active=True,
        )
        db.session.add(agent)
        db.session.flush()
        db.session.add(AgentConfiguration(
            company_id=company.id,
            agent_id=agent.id,
            model="gemini-3.6-flash",
            language="es-AR",
            max_tokens=900,
            temperature=0.20,
            system_prompt="QA commercial agent.",
        ))
        db.session.commit()

        yield {"company": company, "actor": actor}

        db.session.remove()
        db.drop_all()


def test_commercial_phone_gate_is_opt_in_and_exact(commercial_database, monkeypatch):
    assert is_commercial_phone_number_id("COMMERCIAL_PHONE_QA") is True
    assert is_commercial_phone_number_id("ANOTHER_PHONE") is False
    monkeypatch.setenv("WHATSAPP_COMMERCIAL_ENABLED", "0")
    assert is_commercial_phone_number_id("COMMERCIAL_PHONE_QA") is False


def test_commercial_company_resolution_requires_internal_marker(commercial_database):
    company = get_commercial_company()
    assert company is not None
    assert company.name == "StockArMobile Comercial"
    assert company.preferences_json and COMMERCIAL_COMPANY_MARKER in company.preferences_json


def test_commercial_usage_reuses_vendor_entitlement(commercial_database):
    company = commercial_database["company"]
    assert can_use_ai(company, "comercial").allowed is True

    company.preferences_json = json.dumps({
        "internal_channel": COMMERCIAL_COMPANY_MARKER,
        "ai_agent": {"enabled": True, "plan_code": "inicio", "status": "ACTIVA"},
    })
    db.session.commit()
    assert can_use_ai(company, "comercial").allowed is False


def test_capture_inbound_lead_is_tenant_neutral_and_idempotent(commercial_database):
    first_id = capture_inbound_lead("549999000111", "Hola, quiero conocer StockArMobile")
    second_id = capture_inbound_lead("549999000111", "También quisiera saber el precio")
    assert first_id == second_id

    lead = SaaSLead.query.get(first_id)
    assert lead is not None
    assert lead.company_id is None
    assert lead.whatsapp == "549999000111"
    assert lead.source == "whatsapp_comercial"
    assert lead.consent is not None
    assert lead.consent.whatsapp_status == "unknown"
    assert "Hola, quiero conocer StockArMobile" in lead.notes
    assert "También quisiera saber el precio" in lead.notes
    assert SaaSLead.query.filter_by(whatsapp="549999000111").count() == 1


def test_commercial_conversation_uses_dedicated_agent_and_channel(commercial_database):
    company = commercial_database["company"]
    conversation = get_commercial_conversation(company.id, "549999000111")
    assert conversation.channel == "whatsapp_commercial"
    assert conversation.agent_id is not None
    agent = Agent.query.get(conversation.agent_id)
    assert agent is not None
    assert agent.name == COMMERCIAL_AGENT_NAME


def test_commercial_runtime_exposes_only_offer_tool(commercial_database):
    company = commercial_database["company"]
    definitions = AgentRuntime._tool_definitions("comercial", company_id=company.id)
    names = {item["function"]["name"] for item in definitions}
    assert names == {"consultar_oferta_stockarmobile"}
    assert "buscar_producto" not in names
    assert "buscar_cliente" not in names
    assert "preparar_pedido" not in names


def test_commercial_message_can_retry_send_after_runtime_duplicate(commercial_database):
    sends = []
    runtime_calls = []

    def fake_runtime(**kwargs):
        runtime_calls.append(kwargs)
        if len(runtime_calls) == 1:
            return {"status": "completed", "content": "Respuesta comercial QA"}
        return {"status": "duplicate", "content": "Respuesta comercial QA"}

    def fake_send(company, *, to, body):
        sends.append((company.id, to, body))
        return {"status": "accepted"}

    with patch(
        "services.ai_agent.orchestrator_v2.AgentRuntime.process",
        side_effect=fake_runtime,
    ), patch(
        "services.ai_agent.whatsapp_service.WhatsAppService.send_text",
        side_effect=fake_send,
    ):
        first = process_commercial_message(
            phone_number_id="COMMERCIAL_PHONE_QA",
            sender="549999000111",
            external_id="wamid.QA.COMM.001",
            text="Hola",
        )
        second = process_commercial_message(
            phone_number_id="COMMERCIAL_PHONE_QA",
            sender="549999000111",
            external_id="wamid.QA.COMM.001",
            text="Hola",
        )

    assert first["status"] == "completed"
    assert second["status"] == "duplicate"
    assert len(sends) == 2
    assert runtime_calls[0]["company_id"] == commercial_database["company"].id
    assert runtime_calls[0]["channel"] == "whatsapp_commercial"
    assert runtime_calls[0]["metadata"]["commercial_acquisition"] is True
