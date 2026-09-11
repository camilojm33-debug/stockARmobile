"""Delivery worker for queued Vendedor IA WhatsApp follow-ups.

The planner stores outbound work in Conversation.metadata_json["ai_outbox"].
This service is the transport bridge: it re-validates the conversation state,
respects the WhatsApp 24h service window, uses an approved template outside
that window, and records sent/failed/stale delivery state for auditability.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from flask import current_app

from stockarmobile.extensions import db
from stockarmobile.helpers.dates import utcnow_naive
from stockarmobile.models.conversations import Conversation, ConversationMessage
from services.ai_agent.config_service import get_whatsapp_connection, is_ai_enabled
from services.ai_agent.followup_service import (
    AIFollowupService,
    _cart_fingerprint,
    _metadata,
    _outbox,
    _set_metadata,
)
from services.ai_agent.usage_service import can_use_ai
from services.ai_agent.vendor_order_service import VendorOrderService
from services.ai_agent.whatsapp_service import WhatsAppService


OUTBOX_KEY = "ai_outbox"
CART_KEY = "vendor_cart"
PENDING_QUOTE_KEY = "pending_quote_id"
MAX_SENDS_PER_RUN = 100
STALE_AFTER_HOURS = 72
SENDING_LEASE_MINUTES = 15


def _positive_int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    import os

    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def _latest_user_message(conversation: Conversation):
    return (
        ConversationMessage.query.filter(
            ConversationMessage.company_id == int(conversation.company_id),
            ConversationMessage.conversation_id == int(conversation.id),
            ConversationMessage.sender_type == "user",
        )
        .order_by(ConversationMessage.created_at.desc(), ConversationMessage.id.desc())
        .first()
    )


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _template_parameters(conversation: Conversation, cart: dict[str, Any]) -> list[str]:
    state = _metadata(conversation)
    pending_quote_id = state.get(PENDING_QUOTE_KEY)
    if pending_quote_id not in (None, ""):
        try:
            result = VendorOrderService.get_customer_order_status(
                company_id=int(conversation.company_id),
                conversation_id=int(conversation.id),
            )
        except Exception:
            result = {"found": False}
        if result.get("found"):
            return [
                str(result.get("quote_number") or f"P-{int(result['quote_id']):06d}"),
                f"{float(result.get('total') or 0):.2f}",
            ]

    return [
        str(len(cart.get("items") or [])),
        f"{float(cart.get('total') or 0):.2f}",
    ]


def _mutate_item(
    conversation: Conversation,
    dedupe_key: str,
    *,
    status: str,
    now: datetime,
    error: str = "",
    provider_message_id: str = "",
    template_name: str = "",
) -> bool:
    metadata = _metadata(conversation)
    outbox = _outbox(conversation, metadata)
    changed = False
    for item in outbox:
        if str(item.get("dedupe_key") or "") != str(dedupe_key):
            continue
        item["status"] = status
        changed = True
        if status == "sending":
            item["sending_at"] = now.isoformat()
        elif status == "sent":
            item["sent_at"] = now.isoformat()
            if provider_message_id:
                item["provider_message_id"] = provider_message_id
            if template_name:
                item["sent_template_name"] = template_name
        elif status in {"failed", "blocked", "stale"}:
            item["failed_at"] = now.isoformat()
            item["error"] = str(error or "")[:500]
            attempts = int(item.get("attempts") or 0)
            item["attempts"] = attempts
    if changed:
        metadata[OUTBOX_KEY] = outbox[-50:]
        _set_metadata(conversation, metadata)
    return changed


def _record_failure(conversation: Conversation, dedupe_key: str, *, now: datetime, error: str) -> None:
    metadata = _metadata(conversation)
    outbox = _outbox(conversation, metadata)
    for item in outbox:
        if str(item.get("dedupe_key") or "") != str(dedupe_key):
            continue
        attempts = int(item.get("attempts") or 0) + 1
        item["attempts"] = attempts
        item["status"] = "pending"
        item["last_error"] = str(error or "")[:500]
        item["failed_at"] = now.isoformat()
        # Backoff: 15m, 30m, 1h, 2h, 4h, 6h max.
        delay_minutes = min(360, 15 * (2 ** max(0, attempts - 1)))
        item["next_attempt_at"] = (now + timedelta(minutes=delay_minutes)).isoformat()
        break
    metadata[OUTBOX_KEY] = outbox[-50:]
    _set_metadata(conversation, metadata)
    db.session.commit()


def _mark_sent_and_audit(
    conversation: Conversation,
    item: dict[str, Any],
    *,
    now: datetime,
    provider_message_id: str,
    template_name: str = "",
) -> None:
    metadata = _metadata(conversation)
    outbox = _outbox(conversation, metadata)
    for row in outbox:
        if str(row.get("dedupe_key") or "") == str(item.get("dedupe_key") or ""):
            row["status"] = "sent"
            row["sent_at"] = now.isoformat()
            row["provider_message_id"] = provider_message_id or ""
            if template_name:
                row["sent_template_name"] = template_name
            row.pop("last_error", None)
            row.pop("next_attempt_at", None)
            break

    assistant = ConversationMessage(
        conversation_id=conversation.id,
        company_id=conversation.company_id,
        sender_type="agent",
        sender_id=conversation.agent_id,
        role="assistant",
        content=str(item.get("body") or ""),
        content_type="text",
        external_message_id=provider_message_id or None,
        metadata_json={
            "channel": "whatsapp",
            "automated_followup": True,
            "followup_type": item.get("type"),
            "followup_stage": item.get("stage"),
            "dedupe_key": item.get("dedupe_key"),
            "template_name": template_name or None,
            "provider_message_id": provider_message_id or None,
        },
    )
    db.session.add(assistant)
    metadata[OUTBOX_KEY] = outbox[-50:]
    _set_metadata(conversation, metadata)
    db.session.commit()


class AIFollowupDeliveryService:
    """Deliver planner output without stale orders, duplicate work or policy violations."""

    @classmethod
    def max_sends_per_run(cls) -> int:
        return _positive_int_env("AI_FOLLOWUP_MAX_SENDS_PER_RUN", MAX_SENDS_PER_RUN, 1, 500)

    @classmethod
    def dispatch_pending(cls, *, now: datetime | None = None) -> dict[str, Any]:
        effective_now = (now or utcnow_naive()).replace(tzinfo=None)
        limit = cls.max_sends_per_run()
        conversations = (
            Conversation.query.filter(
                Conversation.channel == "whatsapp",
                Conversation.status == "open",
            )
            .order_by(Conversation.updated_at.asc(), Conversation.id.asc())
            .limit(1000)
            .all()
        )

        stats = {
            "scanned": len(conversations),
            "sent": 0,
            "stale": 0,
            "blocked": 0,
            "failed": 0,
            "pending": 0,
            "skipped": 0,
        }

        for conversation in conversations:
            if stats["sent"] >= limit:
                break
            metadata = _metadata(conversation)
            outbox = _outbox(conversation, metadata)
            pending = [
                item
                for item in outbox
                if str(item.get("status") or "pending") == "pending"
                and (
                    not _parse_dt(item.get("next_attempt_at"))
                    or _parse_dt(item.get("next_attempt_at")) <= effective_now
                )
            ]
            if not pending:
                continue

            company = conversation.company
            if company is None or not getattr(company, "active", True):
                stats["skipped"] += len(pending)
                continue

            access = can_use_ai(company, "vendedor")
            connection = get_whatsapp_connection(company)
            ai_enabled = is_ai_enabled(company)
            if not ai_enabled or not connection["enabled"] or not connection["phone_number_id"] or not connection["access_token"]:
                stats["blocked"] += len(pending)
                continue
            if not access.allowed:
                stats["blocked"] += len(pending)
                continue

            latest = _latest_user_message(conversation)
            if latest is None or latest.created_at is None:
                stats["skipped"] += len(pending)
                continue
            last_activity = latest.created_at.replace(tzinfo=None)
            idle = effective_now - last_activity
            if idle > timedelta(hours=STALE_AFTER_HOURS):
                for item in pending:
                    _mutate_item(
                        conversation,
                        str(item.get("dedupe_key") or ""),
                        status="stale",
                        now=effective_now,
                        error="Seguimiento vencido por inactividad prolongada.",
                    )
                db.session.commit()
                stats["stale"] += len(pending)
                continue

            cart = VendorOrderService.get_cart(
                company_id=int(conversation.company_id),
                conversation_id=int(conversation.id),
            )
            if not cart.get("items"):
                for item in pending:
                    _mutate_item(
                        conversation,
                        str(item.get("dedupe_key") or ""),
                        status="stale",
                        now=effective_now,
                        error="El carrito ya no contiene productos.",
                    )
                db.session.commit()
                stats["stale"] += len(pending)
                continue

            current_fingerprint = _cart_fingerprint(cart)

            state = _metadata(conversation)
            pending_quote_id = state.get(PENDING_QUOTE_KEY)
            if pending_quote_id not in (None, ""):
                try:
                    order = VendorOrderService.get_customer_order_status(
                        company_id=int(conversation.company_id),
                        conversation_id=int(conversation.id),
                    )
                except Exception:
                    order = {"found": False}
                if order.get("found") and order.get("order_status") in {"pagado", "confirmado", "pago_con_incidencia"}:
                    for item in pending:
                        _mutate_item(
                            conversation,
                            str(item.get("dedupe_key") or ""),
                            status="stale",
                            now=effective_now,
                            error="El pedido ya no necesita seguimiento.",
                        )
                    db.session.commit()
                    stats["stale"] += len(pending)
                    continue

            for item in pending:
                if stats["sent"] >= limit:
                    break
                dedupe_key = str(item.get("dedupe_key") or "").strip()
                if not dedupe_key:
                    continue

                if int(item.get("anchor_message_id") or 0) != int(latest.id):
                    _mutate_item(
                        conversation,
                        dedupe_key,
                        status="stale",
                        now=effective_now,
                        error="El cliente volvió a interactuar; el seguimiento quedó obsoleto.",
                    )
                    db.session.commit()
                    stats["stale"] += 1
                    continue

                if str(item.get("cart_fingerprint") or "") != current_fingerprint:
                    _mutate_item(
                        conversation,
                        dedupe_key,
                        status="stale",
                        now=effective_now,
                        error="El carrito cambió después de generar el seguimiento.",
                    )
                    db.session.commit()
                    stats["stale"] += 1
                    continue

                # Claim one item before external I/O. A lease makes crash recovery possible.
                metadata = _metadata(conversation)
                outbox = _outbox(conversation, metadata)
                target = next((row for row in outbox if str(row.get("dedupe_key") or "") == dedupe_key), None)
                if target is None or str(target.get("status") or "pending") != "pending":
                    continue
                sending_at = _parse_dt(target.get("sending_at"))
                if sending_at is not None and effective_now - sending_at < timedelta(minutes=SENDING_LEASE_MINUTES):
                    continue
                target["status"] = "sending"
                target["sending_at"] = effective_now.isoformat()
                metadata[OUTBOX_KEY] = outbox[-50:]
                _set_metadata(conversation, metadata)
                db.session.commit()

                try:
                    requires_template = effective_now - last_activity >= timedelta(hours=24)
                    template_name = str(connection.get("template_name") or "").strip()
                    template_language = str(connection.get("template_language") or "es_AR").strip() or "es_AR"

                    if requires_template:
                        if not template_name:
                            raise RuntimeError(
                                "No hay una plantilla de WhatsApp configurada para seguimientos fuera de la ventana de 24 horas."
                            )
                        parameters = _template_parameters(conversation, cart)
                        result = WhatsAppService.send_template(
                            company,
                            to=str(conversation.external_conversation_id or ""),
                            template_name=template_name,
                            template_language=template_language,
                            body_parameters=parameters,
                        )
                    else:
                        result = WhatsAppService.send_text(
                            company,
                            to=str(conversation.external_conversation_id or ""),
                            body=str(target.get("body") or ""),
                        )

                    provider_message_id = ""
                    if isinstance(result, dict):
                        messages = result.get("messages")
                        if isinstance(messages, list) and messages and isinstance(messages[0], dict):
                            provider_message_id = str(messages[0].get("id") or "").strip()
                    _mark_sent_and_audit(
                        conversation,
                        target,
                        now=utcnow_naive(),
                        provider_message_id=provider_message_id,
                        template_name=template_name if requires_template else "",
                    )
                    stats["sent"] += 1
                except Exception as exc:
                    current_app.logger.exception(
                        "AI follow-up delivery failed conversation=%s dedupe=%s",
                        conversation.id,
                        dedupe_key,
                    )
                    _record_failure(
                        conversation,
                        dedupe_key,
                        now=utcnow_naive(),
                        error=str(exc),
                    )
                    stats["failed"] += 1

        return stats
