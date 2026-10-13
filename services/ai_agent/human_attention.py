"""Tenant-scoped human handoff for the WhatsApp Vendedor IA.

The commercial SuperAdmin WhatsApp uses its own isolated implementation. This
module intentionally works only with a tenant's existing WhatsApp conversation.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

from stockarmobile.extensions import db
from stockarmobile.helpers.dates import utcnow_naive
from stockarmobile.models.conversations import Conversation, ConversationMessage


ATTENTION_KEY = "ai_attention"
OUTBOX_KEY = "ai_outbox"
HUMAN_STATUSES = {"human"}


def conversation_metadata(conversation) -> dict[str, Any]:
    raw = getattr(conversation, "metadata_json", None)
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            value = {}
        return value if isinstance(value, dict) else {}
    return {}


def attention_for(conversation) -> dict[str, Any]:
    raw = conversation_metadata(conversation).get(ATTENTION_KEY)
    attention = dict(raw) if isinstance(raw, dict) else {}
    status = str(attention.get("status") or "").strip().lower()
    if status not in {"human", "resolved"}:
        status = "ai"
    taken_by = attention.get("taken_by_user_id")
    return {
        **attention,
        "status": status,
        "pending": status == "human" and not taken_by,
        "taken_by_user_id": taken_by,
        "reason": str(attention.get("reason") or "").strip(),
    }


def _block_pending_outbox(metadata: dict[str, Any], *, reason: str, now: str) -> None:
    rows = metadata.get(OUTBOX_KEY)
    if not isinstance(rows, list):
        return
    for item in rows:
        if not isinstance(item, dict):
            continue
        if str(item.get("status") or "pending").lower() == "pending":
            item["status"] = "blocked"
            item["blocked_at"] = now
            item["error"] = reason


def set_attention(
    conversation,
    *,
    status: str,
    user_id: int | None = None,
    reason: str = "",
) -> dict[str, Any]:
    """Persist an attention state and block follow-ups on human/resolved states."""
    status = str(status or "").strip().lower()
    if status not in {"human", "resolved"}:
        raise ValueError("Estado de atención inválido.")

    metadata = conversation_metadata(conversation)
    current = metadata.get(ATTENTION_KEY)
    payload = dict(current) if isinstance(current, dict) else {}
    now = utcnow_naive().isoformat(timespec="seconds")

    if status == "human":
        payload["status"] = "human"
        payload.setdefault("requested_at", now)
        if reason:
            payload["reason"] = str(reason).strip()[:500]
        if user_id is not None:
            existing_taken_by = payload.get("taken_by_user_id")
            if existing_taken_by and int(existing_taken_by) != int(user_id):
                raise PermissionError("La conversación ya está asignada a otro operador.")
            payload["taken_by_user_id"] = int(user_id)
            payload["taken_at"] = payload.get("taken_at") or now
    else:
        payload.update({
            "status": "resolved",
            "resolved_at": now,
        })
        if user_id is not None:
            payload["resolved_by_user_id"] = int(user_id)

    metadata[ATTENTION_KEY] = payload
    _block_pending_outbox(
        metadata,
        reason="Atención humana activa o resuelta; seguimiento automático bloqueado.",
        now=now,
    )
    conversation.metadata_json = metadata
    conversation.updated_at = utcnow_naive()
    return payload


def resume_ai(conversation, *, user_id: int | None = None) -> None:
    """Return control to AI without reviving reminders blocked during handoff."""
    metadata = conversation_metadata(conversation)
    metadata.pop(ATTENTION_KEY, None)
    conversation.metadata_json = metadata
    conversation.updated_at = utcnow_naive()


def persist_human_inbound(
    conversation,
    *,
    external_id: str,
    sender: str,
    text: str,
) -> ConversationMessage:
    """Store an inbound message without invoking or replying with the AI runtime.

    External message IDs provide idempotency. Existing inbound messages are
    marked as received during human attention so Meta webhook retries never
    cause a delayed AI answer after the operator later resumes the agent.
    """
    external_id = str(external_id or "").strip()
    existing = ConversationMessage.query.filter_by(
        company_id=int(conversation.company_id),
        conversation_id=int(conversation.id),
        external_message_id=external_id,
        role="user",
    ).first() if external_id else None

    if existing is not None:
        message_metadata = dict(existing.metadata_json or {}) if isinstance(existing.metadata_json, dict) else {}
        message_metadata.update({
            "channel": "whatsapp",
            "human_attention": True,
            "human_attention_received_at": utcnow_naive().isoformat(timespec="seconds"),
        })
        existing.metadata_json = message_metadata
        conversation.updated_at = utcnow_naive()
        return existing

    incoming = ConversationMessage(
        company_id=int(conversation.company_id),
        conversation_id=int(conversation.id),
        sender_type="user",
        role="user",
        content=str(text or "")[:4096],
        content_type="text",
        external_message_id=external_id or f"human-inbound:{uuid.uuid4().hex}",
        idempotency_key=f"whatsapp:{external_id}"[:120] if external_id else None,
        trace_id=uuid.uuid4().hex,
        metadata_json={
            "channel": "whatsapp",
            "from": str(sender or "")[:80],
            "human_attention": True,
            "human_attention_received_at": utcnow_naive().isoformat(timespec="seconds"),
        },
    )
    db.session.add(incoming)
    conversation.updated_at = utcnow_naive()
    db.session.flush()
    return incoming


def is_human_paused(conversation) -> bool:
    return attention_for(conversation)["status"] == "human"
