"""WhatsApp Cloud API channel adapter for the 24h vendor agent."""

from __future__ import annotations

import hashlib
import hmac
import os

from flask import Blueprint, abort, current_app, jsonify, render_template, request
from flask_login import current_user, login_required
from sqlalchemy.exc import IntegrityError
from stockarmobile.extensions import db
from stockarmobile.models.conversations import Conversation, ConversationMessage
from services.ai_agent.config_service import company_for_whatsapp_phone_id, get_whatsapp_connection, is_ai_enabled, choose_agent
from services.ai_agent.orchestrator_v2 import AgentRuntime
from services.ai_agent.usage_service import can_use_ai
from services.ai_agent.vendor_order_service import VendorOrderService
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
    conversation = db.session.query(Conversation).filter(
        Conversation.company_id == company_id,
        Conversation.agent_id == agent.id,
        Conversation.channel == "whatsapp",
        Conversation.external_conversation_id == sender,
    ).first()
    if conversation is None:
        conversation = Conversation(company_id=company_id, agent_id=agent.id, channel="whatsapp", external_conversation_id=sender, metadata_json={"whatsapp_user": sender})
        db.session.add(conversation)
        db.session.flush()
    return conversation


def _handle_vendor_command(company_id: int, conversation_id: int, sender: str, text: str):
    """Deterministic commands keep money/stock actions outside the LLM."""
    normalized = " ".join(text.lower().strip().split())
    if normalized in {"carrito", "ver carrito", "mi carrito", "pedido"}:
        cart = VendorOrderService.get_cart(company_id=company_id, conversation_id=conversation_id)
        if not cart["items"]:
            return "Tu carrito está vacío. Decime qué producto querés agregar."
        lines = [f"• {row['quantity']:g} x {row['name']} — ${row['subtotal']:.2f}" for row in cart["items"]]
        return "🛒 *Tu carrito*\n" + "\n".join(lines) + f"\n\n*Total: ${cart['total']:.2f} ARS*\n\nCuando quieras pagar, escribí *pagar*."
    if normalized in {"vaciar carrito", "borrar carrito", "cancelar pedido"}:
        VendorOrderService.update_cart(company_id=company_id, conversation_id=conversation_id, clear=True)
        return "Listo, vacié tu carrito."
    if normalized in {"pagar", "quiero pagar", "confirmar pedido", "confirmar compra"}:
        result = VendorOrderService.create_pending_order(company_id=company_id, conversation_id=conversation_id, customer_phone=sender)
        return f"Perfecto. Tu pedido *{result['quote_number']}* suma *${result['total']:.2f} ARS*.\n\nPagalo acá: {result['payment_url']}\n\nUna vez aprobado el pago, StockARmobile confirma la venta y descuenta el stock automáticamente."
    return None


def _persist_deterministic_turn(conversation, *, external_id: str, text: str, response: str) -> None:
    """Persist successful WhatsApp commands for a complete, auditable conversation."""
    incoming = ConversationMessage(
        conversation_id=conversation.id,
        company_id=conversation.company_id,
        sender_type="user",
        role="user",
        content=text,
        content_type="text",
        external_message_id=external_id,
        idempotency_key=f"whatsapp:{external_id}",
        metadata_json={"channel": "whatsapp", "deterministic_command": True},
    )
    db.session.add(incoming)
    db.session.flush()
    assistant = ConversationMessage(
        conversation_id=conversation.id,
        company_id=conversation.company_id,
        sender_type="agent",
        sender_id=conversation.agent_id,
        role="assistant",
        content=response,
        content_type="text",
        metadata_json={"channel": "whatsapp", "deterministic_command": True, "agent_key": "vendedor"},
    )
    db.session.add(assistant)
    db.session.commit()


def _ai_order_payment(company_id: int, quote_id: int):
    from app import Payment
    prefix = f"flow:ai_order|company_id:{int(company_id)}|quote_id:{int(quote_id)}|"
    return (
        Payment.query.filter(
            Payment.company_id == int(company_id),
            Payment.provider == "mercadopago_ai_order",
            Payment.external_reference.like(prefix + "%"),
        )
        .order_by(Payment.id.desc())
        .first()
    )


def _ai_order_row(company_id: int, quote):
    payment = _ai_order_payment(company_id, quote.id)
    payment_status = str(getattr(payment, "status", "pending") or "pending").lower()
    if getattr(quote, "converted_sale_id", None):
        order_key, order_label, order_badge = "confirmed", "Confirmado", "text-bg-success"
    elif payment_status == "approved":
        order_key, order_label, order_badge = "paid", "Pagado · pendiente de venta", "text-bg-warning"
    elif payment_status in {"pending", "in_process", "authorized", ""}:
        order_key, order_label, order_badge = "pending", "Esperando pago", "text-bg-warning"
    else:
        order_key, order_label, order_badge = "problem", "Incidencia de pago", "text-bg-danger"
    payment_labels = {
        "approved": ("Pagado", "text-bg-success"),
        "pending": ("Pendiente", "text-bg-warning"),
        "in_process": ("En proceso", "text-bg-info"),
        "authorized": ("Autorizado", "text-bg-info"),
        "rejected": ("Rechazado", "text-bg-danger"),
        "cancelled": ("Cancelado", "text-bg-danger"),
        "canceled": ("Cancelado", "text-bg-danger"),
        "refunded": ("Reembolsado", "text-bg-secondary"),
        "charged_back": ("Contracargo", "text-bg-danger"),
    }
    payment_label, payment_badge = payment_labels.get(payment_status, (payment_status.replace("_", " ").title(), "text-bg-secondary"))
    client = getattr(quote, "client", None)
    return {
        "quote_id": quote.id,
        "number": quote.number or f"P-{quote.id:06d}",
        "customer_name": getattr(client, "name", None) or getattr(quote, "consumer_name", None) or "Consumidor final",
        "customer_phone": getattr(client, "whatsapp", None) or getattr(client, "phone", None) or "",
        "total": float(quote.total_amount or 0),
        "currency": quote.currency or "ARS",
        "line_count": len(getattr(quote, "items", []) or []),
        "payment_status": payment_status,
        "payment_label": payment_label,
        "payment_badge": payment_badge,
        "payment_id": getattr(payment, "payment_id", None) if payment else None,
        "paid_at": getattr(payment, "paid_at", None) if payment else None,
        "order_key": order_key,
        "order_label": order_label,
        "order_badge": order_badge,
        "sale_id": quote.converted_sale_id,
        "created_at": quote.date or quote.created_at,
    }


@bp.get("/pedidos-ia")
@login_required
def ai_orders():
    company_id = getattr(current_user, "company_id", None)
    if not company_id or getattr(current_user, "role", None) not in {"admin", "user"}:
        abort(403)
    from app import Quote
    marker = "Pedido generado por el Vendedor 24 hs de StockARmobile."
    quotes = (
        Quote.query.filter(Quote.company_id == int(company_id), Quote.observations == marker)
        .order_by(Quote.date.desc(), Quote.id.desc())
        .limit(100)
        .all()
    )
    all_rows = [_ai_order_row(company_id, quote) for quote in quotes]
    selected_status = (request.args.get("status") or "").strip().lower()
    allowed_filters = {"pending", "paid", "confirmed", "problem"}
    rows = [row for row in all_rows if not selected_status or selected_status not in allowed_filters or row["order_key"] == selected_status]
    summary = {
        "total": len(all_rows),
        "pending": sum(1 for row in all_rows if row["order_key"] == "pending"),
        "paid": sum(1 for row in all_rows if row["payment_status"] == "approved"),
        "confirmed": sum(1 for row in all_rows if row["order_key"] == "confirmed"),
    }
    return render_template("ai_agent/orders.html", orders=rows, summary=summary, selected_status=selected_status if selected_status in allowed_filters else "")


@bp.get("/pedidos-ia/<int:quote_id>")
@login_required
def ai_order_detail(quote_id: int):
    company_id = getattr(current_user, "company_id", None)
    if not company_id or getattr(current_user, "role", None) not in {"admin", "user"}:
        abort(403)
    from app import Quote
    marker = "Pedido generado por el Vendedor 24 hs de StockARmobile."
    quote = Quote.query.filter_by(id=int(quote_id), company_id=int(company_id), observations=marker).first()
    if quote is None:
        abort(404)
    return render_template(
        "ai_agent/order_detail.html",
        order=_ai_order_row(company_id, quote),
        quote=quote,
        payment=_ai_order_payment(company_id, quote.id),
    )


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
    processed, errors = 0, []
    for phone_number_id, sender, external_id, text in _extract_messages(payload):
        try:
            company = company_for_whatsapp_phone_id(phone_number_id)
            if company is None:
                errors.append({"external_message_id": external_id, "error": "company_not_configured"})
                continue

            duplicate = db.session.query(ConversationMessage).filter(
                ConversationMessage.company_id == company.id,
                ConversationMessage.external_message_id == external_id,
            ).first()
            if duplicate is not None:
                continue

            connection = get_whatsapp_connection(company)
            if not connection["enabled"] or not is_ai_enabled(company):
                continue

            conversation = _get_or_create_conversation(company.id, sender)
            access = can_use_ai(company, "vendedor")
            if not access.allowed:
                errors.append({"external_message_id": external_id, "error": "ai_plan_blocked", "reason": access.reason})
                continue

            command_response = _handle_vendor_command(company.id, conversation.id, sender, text)
            if command_response is not None:
                _persist_deterministic_turn(conversation, external_id=external_id, text=text, response=command_response)
                WhatsAppService.send_text(company, to=sender, body=command_response)
                processed += 1
                continue

            idempotency_key = f"whatsapp:{external_id}"
            try:
                result = AgentRuntime.process(
                    company_id=company.id,
                    conversation_id=conversation.id,
                    message=text,
                    channel="whatsapp",
                    external_message_id=external_id,
                    idempotency_key=idempotency_key,
                    metadata={"phone_number_id": phone_number_id, "from": sender, "channel": "whatsapp"},
                )
            except IntegrityError as exc:
                db.session.rollback()
                if "uq_convmsg_company_idempotency" in str(exc.orig) or "uq_convmsg_external_company_conv" in str(exc.orig):
                    continue
                raise

            if result.get("status") == "duplicate":
                continue
            WhatsAppService.send_text(company, to=sender, body=result["content"])
            processed += 1
        except Exception:
            db.session.rollback()
            current_app.logger.exception("WhatsApp agent error external_message_id=%s", external_id)
            errors.append({"external_message_id": external_id, "error": "internal_error"})
    return jsonify({"success": True, "processed": processed, "errors": errors}), 200