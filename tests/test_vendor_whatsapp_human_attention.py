import os
from types import SimpleNamespace
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("MP_OAUTH_ENCRYPTION_KEY", "test-oauth-encryption-key")

import pytest

import app as stock_app
from app import Company, db
from services.ai_agent.followup_service import AIFollowupService
from services.ai_agent.human_attention import (
    attention_for,
    persist_human_inbound,
    resume_ai,
    set_attention,
)
from stockarmobile.models.conversations import Agent, Conversation, ConversationMessage


REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def tenant_whatsapp_conversation():
    stock_app.app.config["TESTING"] = True
    with stock_app.app.app_context():
        db.drop_all()
        db.create_all()
        company = Company(name="Tenant Atención QA", active=True)
        db.session.add(company)
        db.session.flush()
        agent = Agent(company_id=company.id, name="Vendedor 24 hs", active=True)
        db.session.add(agent)
        db.session.flush()
        conversation = Conversation(
            company_id=company.id,
            agent_id=agent.id,
            channel="whatsapp",
            external_conversation_id="5491112345678",
            status="open",
            metadata_json={"ai_outbox": [
                {"dedupe_key": "pending-one", "status": "pending"},
                {"dedupe_key": "already-sent", "status": "sent"},
            ]},
        )
        db.session.add(conversation)
        db.session.commit()
        yield conversation
        db.session.remove()
        db.drop_all()


def test_take_conversation_pauses_ai_and_blocks_pending_reminders():
    conversation = SimpleNamespace(
        company_id=12,
        id=34,
        metadata_json={
            "ai_outbox": [
                {"dedupe_key": "pending", "status": "pending"},
                {"dedupe_key": "sent", "status": "sent"},
            ]
        },
        updated_at=None,
    )

    set_attention(
        conversation,
        status="human",
        user_id=88,
        reason="Cliente pidió una persona.",
    )

    state = attention_for(conversation)
    assert state["status"] == "human"
    assert state["pending"] is False
    assert state["taken_by_user_id"] == 88
    assert state["reason"] == "Cliente pidió una persona."
    assert conversation.metadata_json["ai_outbox"][0]["status"] == "blocked"
    assert conversation.metadata_json["ai_outbox"][1]["status"] == "sent"


def test_conversation_cannot_be_taken_by_two_different_operators():
    conversation = SimpleNamespace(
        company_id=12,
        id=34,
        metadata_json={},
        updated_at=None,
    )
    set_attention(conversation, status="human", user_id=88)

    with pytest.raises(PermissionError, match="asignada a otro operador"):
        set_attention(conversation, status="human", user_id=99)


@pytest.mark.parametrize("status", ["human", "resolved"])
def test_pending_followup_queue_is_suppressed_during_human_attention(status):
    conversation = SimpleNamespace(
        company_id=12,
        id=34,
        metadata_json={
            "ai_attention": {"status": status},
            "ai_outbox": [{"dedupe_key": "queued", "status": "pending"}],
        },
    )

    assert AIFollowupService.pending_for_conversation(conversation) == []


def test_resume_ai_removes_attention_state_without_reviving_blocked_outbox():
    conversation = SimpleNamespace(
        company_id=12,
        id=34,
        metadata_json={
            "ai_attention": {"status": "human", "taken_by_user_id": 88},
            "ai_outbox": [{"dedupe_key": "queued", "status": "blocked"}],
        },
        updated_at=None,
    )

    resume_ai(conversation, user_id=88)

    assert "ai_attention" not in conversation.metadata_json
    assert conversation.metadata_json["ai_outbox"][0]["status"] == "blocked"


def test_human_owned_inbound_message_is_persisted_once(tenant_whatsapp_conversation):
    conversation = tenant_whatsapp_conversation

    first = persist_human_inbound(
        conversation,
        external_id="wamid.human-attention-001",
        sender="5491112345678",
        text="Necesito hablar con una persona.",
    )
    db.session.commit()

    duplicate = persist_human_inbound(
        conversation,
        external_id="wamid.human-attention-001",
        sender="5491112345678",
        text="Texto duplicado del webhook",
    )
    db.session.commit()

    assert first.id == duplicate.id
    assert ConversationMessage.query.filter_by(
        company_id=conversation.company_id,
        conversation_id=conversation.id,
        external_message_id="wamid.human-attention-001",
        role="user",
    ).count() == 1
    assert duplicate.content == "Necesito hablar con una persona."
    assert duplicate.metadata_json["human_attention"] is True


def test_human_attention_panel_is_tenant_scoped_and_has_human_reply_route():
    source = (REPO_ROOT / "whatsapp_agent.py").read_text(encoding="utf-8")
    template = (REPO_ROOT / "templates/ai_agent/whatsapp_attention.html").read_text(encoding="utf-8")

    assert '@bp.get("/pedidos-ia/atencion-whatsapp")' in source
    assert '@bp.post("/pedidos-ia/atencion-whatsapp/<int:conversation_id>/action")' in source
    assert '@bp.post("/pedidos-ia/atencion-whatsapp/<int:conversation_id>/mensaje")' in source
    assert 'company_id=int(company_id)' in source
    assert 'channel="whatsapp"' in source
    assert 'sender_type="human"' in source
    assert 'WhatsAppService.send_text(company, to=recipient, body=body)' in source
    assert "Tomar conversación" in template
    assert "Reanudar IA" in template
    assert "Enviar mensaje" in template
