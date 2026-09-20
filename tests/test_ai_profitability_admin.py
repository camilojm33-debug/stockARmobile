import json

import pytest

import app as stock_app
from app import Company, User, db
from stockarmobile.models.conversations import Conversation, ConversationMessage


@pytest.fixture
def ai_profitability_app():
    stock_app.app.config["TESTING"] = True
    stock_app.app.config["WTF_CSRF_ENABLED"] = False
    stock_app.app.config["SERVER_NAME"] = "localhost"
    with stock_app.app.app_context():
        db.drop_all()
        db.create_all()
        yield stock_app.app
        db.session.remove()
        db.drop_all()


def test_superadmin_ai_profitability_panel_renders(ai_profitability_app, monkeypatch):
    monkeypatch.setenv("AI_TOKEN_PRICING_JSON", json.dumps({"gemini:gemini-3.6-flash": {"input_per_1m_usd": 0.75, "output_per_1m_usd": 3.75}}))
    monkeypatch.setenv("AI_USD_TO_ARS", "1514.5")
    with ai_profitability_app.app_context():
        company = Company(name="Empresa Rentabilidad", active=True, contact_email="rentabilidad@test.local")
        db.session.add(company)
        db.session.flush()
        superadmin = User(username="superadmin_costos", email="superadmin_costos@test.local", role="superadmin", active=True, company_id=company.id, password_hash="x")
        db.session.add(superadmin)
        db.session.flush()
        conversation = Conversation(company_id=company.id, channel="test", status="open")
        db.session.add(conversation)
        db.session.flush()
        message = ConversationMessage(company_id=company.id, conversation_id=conversation.id, sender_type="agent", role="assistant", content="Respuesta IA", content_type="text", metadata_json={"ai_usage_recorded": True, "agent_key": "asistente", "ai_usage_telemetry": {"provider": "gemini", "model": "gemini-3.6-flash", "input_tokens": 1000, "output_tokens": 500, "total_tokens": 1500, "provider_calls": 1, "tool_rounds": 0, "cost": {"priced": True, "estimated_cost_usd": 0.002625}}})
        db.session.add(message)
        db.session.commit()
        client = ai_profitability_app.test_client()
        with client.session_transaction() as session:
            session["_user_id"] = str(superadmin.id)
        response = client.get("/superadmin/ai-profitability")
        assert response.status_code == 200
        html = response.get_data(as_text=True)
        assert "Costos y Rentabilidad IA" in html
        assert "gemini:gemini-3.6-flash" in html
        assert "Empresa Rentabilidad" in html