"""WhatsApp Cloud API channel adapter for the 24h vendor agent."""

from __future__ import annotations

import hashlib
import hmac
import os

from flask import Blueprint, current_app, jsonify, request

from stockarmobile.extensions import db
from stockarmobile.models.conversations import Conversation
from services.ai_agent.config_service import company_for_whatsapp_phone_id, get_whatsapp_connection, is_ai_enabled, choose_agent
from services.ai_agent.orchestrator_v2 import AgentRuntime
from services.ai_agent.whatsapp_service import WhatsAppService

bp = Blueprint("whatsapp_agent", __name__)


def _verify_signature(raw_body: bytes) -> bool:
    app_secret = (os.getenv("WHATSAPP_APP_SECRET") or "").strip()
    if not app_secret:
        return not bool(current_app.config.get("IS_PRODUCTION_ENV"))
    header = request.headers.get("X-Hub-Signature-256", "")
    if not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header.split("=", 1)[1], expected)


def _extract_messages(payload):
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            metadata = value.get("metadata") or {}
            phone_number_id = str(metadata.get("phone_number_id") or "").strip()
            for message in value.get("messages") or []:
                if message.get("type") != "text":
                    continue
                text = ((message.get("text") or {}).get("body") or "").strip()
                sender = str(message.get("from") or "").strip()
                external_id = str(message.get("id") or "").strip()
                if phone_number_id and sender and text and external_id:
                    yield phone_number_id, sender, external_id, text


def _get_or_create_conversation(company_id: int, sender: str):
    agent = choose_agent(company_id, channel="whatsapp")
    conversation = (
        db.session.query(Conversation)
        .filter(
            Conversation.company_id == company_id,
            Conversation.agent_id == agent.id,
            Conversation.channel == "whatsapp",
            Conversation.external_thread_id == sender,
        )
        .first()
    )
    if conversation is None:
        conversation = Conversation(
            company_id=company_id,
            agent_id=agent.id,
            channel="whatsapp",
            external_thread_id=sender,
        )
        db.session.add(conversation)
        db.session.flush()
    return conversation


@bp.route("/api/whatsapp/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        expected = os.getenv("WHATSAPP_VERIFY_TOKEN", "")
        if mode == "subscribe" and expected and hmac.compare_digest(str(token or ""), expected):
            return challenge or "", 200
        return "Forbidden", 403

    raw = request.get_data(cache=True) or b""
    if not _verify_signature(raw):
        return jsonify({"success": False, "error": "invalid_signature"}), 401
    payload = request.get_json(silent=True) or {}
    processed = 0
    errors = []
    for phone_number_id, sender, external_id, text in _extract_messages(payload):
        try:
            company = company_for_whatsapp_phone_id(phone_number_id)
            if company is None:
                errors.append({"external_message_id": external_id, "error": "company_not_configured"})
                continue
            connection = get_whatsapp_connection(company)
            if not connection["enabled"] or not is_ai_enabled(company):
                continue
            conversation = _get_or_create_conversation(company.id, sender)
            result = AgentRuntime.process(
                company_id=company.id,
                conversation_id=conversation.id,
                message=text,
                channel="whatsapp",
                external_message_id=external_id,
                idempotency_key=f"whatsapp:{external_id}",
                metadata={"phone_number_id": phone_number_id, "from": sender, "channel": "whatsapp"},
            )
            if result.get("status") == "duplicate":
                continue
            WhatsAppService.send_text(company, to=sender, body=result["content"])
            processed += 1
        except Exception as exc:
            db.session.rollback()
            current_app.logger.exception("WhatsApp agent error external_message_id=%s", external_id)
            errors.append({"external_message_id": external_id, "error": str(exc)[:300]})
    return jsonify({"success": True, "processed": processed, "errors": errors}), 200
