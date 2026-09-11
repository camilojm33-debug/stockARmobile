"""Proactive follow-up planner for the 24/7 vendor agent.

The service intentionally queues outbound follow-ups instead of sending them.
This keeps WhatsApp transport/policy concerns separate and lets the future
WhatsApp connector consume the outbox when the merchant is configured.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta
from typing import Any

from stockarmobile.extensions import db
from stockarmobile.helpers.dates import utcnow_naive
from stockarmobile.models.conversations import Conversation, ConversationMessage
from services.ai_agent.vendor_order_service import VendorOrderService


OUTBOX_KEY = "ai_outbox"
CART_KEY = "vendor_cart"
PENDING_QUOTE_KEY = "pending_quote_id"
PENDING_PAYMENT_KEY = "pending_payment_url"


def _metadata(conversation) -> dict[str, Any]:
    raw = conversation.metadata_json or {}
    if isinstance(raw, dict):
        return dict(raw)
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else {}
    except json.JSONDecodeError:
        parsed = {}
    return parsed if isinstance(parsed, dict) else {}


def _set_metadata(conversation, payload: dict[str, Any]) -> None:
    conversation.metadata_json = payload


def _cart_fingerprint(cart: dict[str, Any]) -> str:
    payload = [
        {
            "product_id": int(item["product_id"]),
            "quantity": float(item["quantity"]),
            "unit_price": float(item["unit_price"]),
        }
        for item in sorted(cart.get("items") or [], key=lambda row: int(row["product_id"]))
    ]
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _positive_int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def _outbox(conversation, metadata: dict[str, Any]) -> list[dict[str, Any]]:
    rows = metadata.get(OUTBOX_KEY)
    if not isinstance(rows, list):
        rows = []
    # Bound metadata growth while preserving recent delivery history.
    rows = [row for row in rows if isinstance(row, dict)][-50:]
    return rows


def _has_queued(outbox: list[dict[str, Any]], dedupe_key: str) -> bool:
    return any(
        str(item.get("dedupe_key") or "") == dedupe_key
        and str(item.get("status") or "pending") in {"pending", "sent"}
        for item in outbox
    )


def _latest_user_message(conversation_id: int, company_id: int):
    return (
        ConversationMessage.query.filter(
            ConversationMessage.company_id == int(company_id),
            ConversationMessage.conversation_id == int(conversation_id),
            ConversationMessage.sender_type == "user",
        )
        .order_by(ConversationMessage.created_at.desc(), ConversationMessage.id.desc())
        .first()
    )


def _build_message(*, stage: int, cart: dict[str, Any], pending_quote_id: Any, payment_url: str) -> tuple[str, str]:
    items = cart.get("items") or []
    item_count = len(items)
    total = float(cart.get("total") or 0)
    quote_number = None
    if pending_quote_id not in (None, ""):
        try:
            quote_number = f"P-{int(pending_quote_id):06d}"
        except (TypeError, ValueError):
            quote_number = None

    if quote_number:
        title = "pending_payment"
        if stage == 1:
            body = (
                f"Hola 👋 Tu pedido {quote_number} sigue listo para completar. "
                f"Son {item_count} producto(s) por ${total:.2f} ARS."
            )
            if payment_url:
                body += f"\n\nPodés pagar acá: {payment_url}"
            else:
                body += "\n\nEscribime *pagar* y te ayudo a continuar."
        else:
            body = (
                f"Hola 👋 Te dejo un último recordatorio sobre tu pedido {quote_number} "
                f"por ${total:.2f} ARS. Cuando quieras retomarlo, escribime *pedido* o *reintentar pago*."
            )
        return title, body

    title = "abandoned_cart"
    if stage == 1:
        body = (
            f"Hola 👋 Vimos que dejaste {item_count} producto(s) en tu carrito por ${total:.2f} ARS. "
            "¿Querés continuar con tu compra?"
        )
        if item_count == 1:
            name = str(items[0].get("name") or "").strip()
            if name:
                body = f"Hola 👋 Dejaste *{name}* en tu carrito por ${total:.2f} ARS. ¿Querés continuar con tu compra?"
        return title, body

    return title, (
        f"Hola 👋 Último recordatorio: tu carrito sigue guardado con {item_count} producto(s) "
        f"por ${total:.2f} ARS. Cuando quieras retomarlo, escribime *carrito*."
    )


class AIFollowupService:
    """Find inactive WhatsApp buying sessions and queue one reminder at a time."""

    @classmethod
    def settings(cls) -> dict[str, int]:
        return {
            "first_delay_minutes": _positive_int_env("AI_ABANDONED_CART_FIRST_MINUTES", 120, 30, 10080),
            "second_delay_minutes": _positive_int_env("AI_ABANDONED_CART_SECOND_MINUTES", 1440, 120, 20160),
            "max_reminders": _positive_int_env("AI_ABANDONED_CART_MAX_REMINDERS", 2, 1, 2),
            "max_conversations": _positive_int_env("AI_ABANDONED_CART_MAX_CONVERSATIONS", 500, 1, 5000),
        }

    @classmethod
    def scan(cls, *, now: datetime | None = None, dry_run: bool = False) -> dict[str, Any]:
        effective_now = (now or utcnow_naive()).replace(tzinfo=None)
        settings = cls.settings()
        first_cutoff = effective_now - timedelta(minutes=settings["first_delay_minutes"])
        second_cutoff = effective_now - timedelta(minutes=settings["second_delay_minutes"])
        conversations = (
            Conversation.query.filter(
                Conversation.channel == "whatsapp",
                Conversation.status == "open",
            )
            .order_by(Conversation.updated_at.asc(), Conversation.id.asc())
            .limit(settings["max_conversations"])
            .all()
        )

        stats = {
            "scanned": len(conversations),
            "eligible": 0,
            "queued": 0,
            "skipped_no_activity": 0,
            "skipped_no_recipient": 0,
            "skipped_recent": 0,
            "queued_items": [],
            "dry_run": dry_run,
            "settings": settings,
        }

        for conversation in conversations:
            metadata = _metadata(conversation)
            raw_cart = metadata.get(CART_KEY)
            if not isinstance(raw_cart, dict) or not raw_cart:
                continue
            recipient = "".join(ch for ch in str(conversation.external_conversation_id or "") if ch.isdigit())
            if not recipient:
                stats["skipped_no_recipient"] += 1
                continue

            latest = _latest_user_message(conversation.id, conversation.company_id)
            if latest is None or latest.created_at is None:
                stats["skipped_no_activity"] += 1
                continue
            activity_at = latest.created_at.replace(tzinfo=None)
            idle_since = effective_now - activity_at
            if idle_since < timedelta(minutes=settings["first_delay_minutes"]):
                stats["skipped_recent"] += 1
                continue

            cart = VendorOrderService.get_cart(company_id=conversation.company_id, conversation_id=conversation.id)
            if not cart.get("items"):
                continue
            stats["eligible"] += 1
            fingerprint = _cart_fingerprint(cart)
            outbox = _outbox(conversation, metadata)

            stage = None
            if idle_since >= timedelta(minutes=settings["second_delay_minutes"]):
                stage = 2 if settings["max_reminders"] >= 2 else 1
            else:
                stage = 1

            dedupe_key = f"ai_followup:{conversation.company_id}:{conversation.id}:{latest.id}:{fingerprint}:stage{stage}"
            if _has_queued(outbox, dedupe_key):
                continue

            title, body = _build_message(
                stage=stage,
                cart=cart,
                pending_quote_id=metadata.get(PENDING_QUOTE_KEY),
                payment_url=str(metadata.get(PENDING_PAYMENT_KEY) or "").strip(),
            )
            requires_template = idle_since >= timedelta(hours=24)
            payload = {
                "id": dedupe_key,
                "type": title,
                "channel": "whatsapp",
                "status": "pending",
                "dedupe_key": dedupe_key,
                "company_id": int(conversation.company_id),
                "conversation_id": int(conversation.id),
                "to": recipient,
                "stage": stage,
                "requires_template": requires_template,
                "template_name": "" if not requires_template else "abandoned_cart_followup",
                "template_language": "es_AR",
                "body": body,
                "cart_fingerprint": fingerprint,
                "anchor_message_id": int(latest.id),
                "created_at": effective_now.isoformat(),
                "total": float(cart.get("total") or 0),
                "item_count": int(cart.get("line_count") or len(cart.get("items") or [])),
            }
            stats["queued_items"].append(payload)
            stats["queued"] += 1
            if dry_run:
                continue

            outbox.append(payload)
            metadata[OUTBOX_KEY] = outbox[-50:]
            _set_metadata(conversation, metadata)
            db.session.add(conversation)

        if not dry_run:
            db.session.commit()
        return stats

    @classmethod
    def pending_for_conversation(cls, conversation) -> list[dict[str, Any]]:
        metadata = _metadata(conversation)
        return [
            item for item in _outbox(conversation, metadata)
            if str(item.get("status") or "pending") == "pending"
        ]

    @classmethod
    def mark_sent(cls, *, company_id: int, conversation_id: int, dedupe_key: str) -> bool:
        conversation = Conversation.query.filter_by(id=int(conversation_id), company_id=int(company_id)).first()
        if conversation is None:
            return False
        metadata = _metadata(conversation)
        outbox = _outbox(conversation, metadata)
        changed = False
        sent_at = utcnow_naive().isoformat()
        for item in outbox:
            if str(item.get("dedupe_key") or "") == str(dedupe_key):
                item["status"] = "sent"
                item["sent_at"] = sent_at
                changed = True
        if changed:
            metadata[OUTBOX_KEY] = outbox
            _set_metadata(conversation, metadata)
            db.session.commit()
        return changed

    @classmethod
    def mark_failed(cls, *, company_id: int, conversation_id: int, dedupe_key: str, error: str = "") -> bool:
        conversation = Conversation.query.filter_by(id=int(conversation_id), company_id=int(company_id)).first()
        if conversation is None:
            return False
        metadata = _metadata(conversation)
        outbox = _outbox(conversation, metadata)
        changed = False
        for item in outbox:
            if str(item.get("dedupe_key") or "") == str(dedupe_key):
                item["status"] = "failed"
                item["failed_at"] = utcnow_naive().isoformat()
                item["error"] = str(error or "")[:500]
                changed = True
        if changed:
            metadata[OUTBOX_KEY] = outbox
            _set_metadata(conversation, metadata)
            db.session.commit()
        return changed
