from datetime import datetime, timedelta
from types import SimpleNamespace

from app import Company, db
from stockarmobile.models.conversations import Agent, Conversation
from services.ai_agent.followup_delivery_service import (
    SENDING_LEASE_MINUTES,
    _claim_pending_item,
    _recover_expired_sending,
)
from services.ai_agent.followup_service import _has_queued, _outbox


def _create_conversation(metadata):
    company = Company(name="Outbox Recovery QA", active=True)
    db.session.add(company)
    db.session.flush()
    agent = Agent(company_id=company.id, name="Outbox Agent", active=True)
    db.session.add(agent)
    db.session.flush()
    conversation = Conversation(
        company_id=company.id,
        agent_id=agent.id,
        channel="whatsapp",
        external_conversation_id="5491112345678",
        status="open",
        metadata_json=metadata,
    )
    db.session.add(conversation)
    db.session.commit()
    return company.id, conversation.id


def test_expired_sending_moves_to_uncertain_without_automatic_resend(app):
    now = datetime(2026, 10, 25, 12, 0, 0)
    company_id, conversation_id = _create_conversation({
        "ai_outbox": [{
            "dedupe_key": "followup-expired",
            "status": "sending",
            "sending_at": (now - timedelta(minutes=SENDING_LEASE_MINUTES + 1)).isoformat(),
            "body": "Mensaje sintético",
        }]
    })

    recovered = _recover_expired_sending(
        company_id=company_id,
        conversation_id=conversation_id,
        now=now,
    )
    assert recovered == 1

    db.session.expire_all()
    conversation = db.session.get(Conversation, conversation_id)
    item = conversation.metadata_json["ai_outbox"][0]
    assert item["status"] == "uncertain"
    assert item["reconciliation_required"] is True
    assert "antes de considerar un nuevo envío" in item["last_error"]
    assert _has_queued(_outbox(conversation, conversation.metadata_json), "followup-expired") is True

    assert _recover_expired_sending(
        company_id=company_id,
        conversation_id=conversation_id,
        now=now + timedelta(minutes=5),
    ) == 0


def test_fresh_sending_is_not_recovered(app):
    now = datetime(2026, 10, 25, 12, 0, 0)
    company_id, conversation_id = _create_conversation({
        "ai_outbox": [{
            "dedupe_key": "followup-active",
            "status": "sending",
            "sending_at": (now - timedelta(minutes=2)).isoformat(),
        }]
    })

    assert _recover_expired_sending(
        company_id=company_id,
        conversation_id=conversation_id,
        now=now,
    ) == 0
    db.session.expire_all()
    item = db.session.get(Conversation, conversation_id).metadata_json["ai_outbox"][0]
    assert item["status"] == "sending"


def test_pending_item_can_only_be_claimed_once_sequentially(app):
    now = datetime(2026, 10, 25, 12, 0, 0)
    company_id, conversation_id = _create_conversation({
        "ai_outbox": [{
            "dedupe_key": "followup-claim",
            "status": "pending",
            "body": "Mensaje sintético",
        }]
    })

    first = _claim_pending_item(
        company_id=company_id,
        conversation_id=conversation_id,
        dedupe_key="followup-claim",
        now=now,
    )
    second = _claim_pending_item(
        company_id=company_id,
        conversation_id=conversation_id,
        dedupe_key="followup-claim",
        now=now,
    )

    assert first is not None
    assert first["status"] == "sending"
    assert second is None
    db.session.expire_all()
    item = db.session.get(Conversation, conversation_id).metadata_json["ai_outbox"][0]
    assert item["status"] == "sending"


def test_outbox_trimming_preserves_active_work_even_above_history_limit():
    active_rows = [
        {"dedupe_key": f"active-{i}", "status": "uncertain" if i % 2 else "sending"}
        for i in range(60)
    ]
    terminal_rows = [
        {"dedupe_key": f"sent-{i}", "status": "sent"}
        for i in range(70)
    ]
    conversation = SimpleNamespace()
    rows = _outbox(conversation, {"ai_outbox": terminal_rows + active_rows})

    assert len(rows) == 60
    assert {row["dedupe_key"] for row in rows} == {row["dedupe_key"] for row in active_rows}


def test_definitive_http_rejection_can_retry_but_ambiguous_errors_cannot():
    from services.ai_agent.followup_delivery_service import _is_definitive_whatsapp_rejection

    assert _is_definitive_whatsapp_rejection(RuntimeError("WhatsApp API HTTP 400: invalid payload"))
    assert _is_definitive_whatsapp_rejection(RuntimeError("WhatsApp API HTTP 429: rate limited"))
    assert not _is_definitive_whatsapp_rejection(RuntimeError("WhatsApp API HTTP 408: timeout"))
    assert not _is_definitive_whatsapp_rejection(RuntimeError("WhatsApp API HTTP 503: temporary failure"))
    assert not _is_definitive_whatsapp_rejection(RuntimeError("WhatsApp API devolvió JSON inválido."))
    assert not _is_definitive_whatsapp_rejection(TimeoutError("connection timed out"))
