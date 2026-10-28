"""WhatsApp Cloud API channel adapter for the 24h vendor agent."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import uuid

from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy.exc import IntegrityError
from stockarmobile.extensions import csrf, db
from stockarmobile.helpers.dates import utcnow_naive
from stockarmobile.decorators import company_admin_required
from app import get_current_company_id
from stockarmobile.models.conversations import Conversation, ConversationMessage
from services.ai_agent.config_service import company_for_whatsapp_phone_id, get_whatsapp_connection, get_vendor_options, is_ai_enabled, choose_agent
from services.ai_agent.orchestrator_v2 import AgentRuntime, MAX_AGENT_MESSAGE_CHARS
from services.ai_agent.usage_service import can_use_ai
from services.ai_agent.vendor_followup_tools import install_vendor_followup_tools
from services.ai_agent.vendor_order_service import VendorOrderService, _metadata, _set_metadata
from services.ai_agent.human_attention import attention_for, conversation_metadata, persist_human_inbound, resume_ai, set_attention
from services.ai_agent.whatsapp_service import WhatsAppService
from services.saas_commercial_whatsapp import is_commercial_phone_number_id, process_commercial_message

install_vendor_followup_tools(AgentRuntime)

bp = Blueprint("whatsapp_agent", __name__)


def _verify_signature(raw_body: bytes) -> bool:
    # Embedded Signup accepts META_APP_SECRET as the canonical Meta app secret;
    # use the same fallback here so webhook verification matches onboarding.
    app_secret = (os.getenv("META_APP_SECRET") or os.getenv("WHATSAPP_APP_SECRET") or "").strip()
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


def _extract_statuses(payload):
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            metadata = value.get("metadata") or {}
            phone_number_id = str(metadata.get("phone_number_id") or "").strip()
            for status in value.get("statuses") or []:
                message_id = str(status.get("id") or "").strip()
                state = str(status.get("status") or "").strip().lower()
                recipient_id = str(status.get("recipient_id") or "").strip()
                if phone_number_id and message_id and state:
                    yield phone_number_id, message_id, state, recipient_id, status


def _record_commercial_campaign_status(status_rows):
    from app import SaaSCampaignEvent, SaaSCampaignRecipient

    updated = 0
    for phone_number_id, message_id, state, recipient_id, raw_status in status_rows:
        if not is_commercial_phone_number_id(phone_number_id):
            continue
        recipient = SaaSCampaignRecipient.query.filter_by(
            provider_message_id=message_id,
            channel="whatsapp",
        ).first()
        if recipient is None:
            continue

        now = utcnow_naive()
        recipient.provider_status = state
        if state == "delivered" and recipient.delivered_at is None:
            recipient.delivered_at = now
            db.session.add(SaaSCampaignEvent(
                campaign_id=recipient.campaign_id,
                recipient_id=recipient.id,
                event_type="delivered",
                metadata_json='{"channel":"whatsapp"}',
                created_at=now,
            ))
            updated += 1
        elif state == "read" and recipient.opened_at is None:
            recipient.opened_at = now
            db.session.add(SaaSCampaignEvent(
                campaign_id=recipient.campaign_id,
                recipient_id=recipient.id,
                event_type="read",
                metadata_json='{"channel":"whatsapp"}',
                created_at=now,
            ))
            updated += 1
        elif state == "failed":
            errors = raw_status.get("errors") or []
            recipient.error_reason = str(
                errors[0].get("title") or errors[0].get("message") or "WhatsApp rechazó el mensaje."
            )[:2000] if errors else "WhatsApp rechazó el mensaje."
            if recipient.failed_at is None:
                recipient.failed_at = now
                if recipient.status == "sent":
                    recipient.status = "failed"
                    recipient.campaign.failed_count = int(recipient.campaign.failed_count or 0) + 1
                db.session.add(SaaSCampaignEvent(
                    campaign_id=recipient.campaign_id,
                    recipient_id=recipient.id,
                    event_type="failed",
                    metadata_json='{"channel":"whatsapp"}',
                    created_at=now,
                ))
                updated += 1
    return updated


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


def _customer_order_response(company_id: int, conversation_id: int, order_number: str = "") -> str:
    result = VendorOrderService.get_customer_order_status(
        company_id=company_id,
        conversation_id=conversation_id,
        order_number=order_number,
    )
    if not result.get("found"):
        return "Todavía no encuentro un pedido generado por este WhatsApp. Si querés, revisamos tu carrito."
    messages = {
        "confirmado": "✅ tu pedido está confirmado y el pago fue aprobado.",
        "pagado": "💰 tu pago ya figura aprobado y el pedido está pendiente de confirmación de venta.",
        "pendiente_pago": "⏳ tu pedido está esperando el pago.",
        "pago_con_incidencia": "⚠️ el último pago tuvo una incidencia.",
    }
    body = messages.get(result["order_status"], f"tu pedido está en estado {result['order_status']}.")
    extra = f"\n\nPedido: *{result['quote_number']}*"
    if result.get("payment_url") and result["order_status"] == "pendiente_pago":
        extra += f"\nLink de pago: {result['payment_url']}"
    return f"El pedido *{result['quote_number']}* por *${result['total']:.2f} ARS* {body}{extra}"


def _handle_vendor_wholesale_inquiry(company_id: int, conversation_id: int, sender: str, text: str):
    """Answer wholesale questions from the saved quote or resolved cart, never from model guesswork."""
    import unicodedata
    from decimal import Decimal
    from app import Quote, QuoteItem
    from wholesale_pricing import PriceList, price_list_is_current
    from services.ai_agent.vendor_order_service import (
        PENDING_QUOTE_KEY,
        _format_vendor_currency,
        _money,
        format_vendor_wholesale_price_details,
    )

    normalized = unicodedata.normalize("NFKD", str(text or "").strip().lower())
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    normalized = " ".join(normalized.split())
    if not any(term in normalized for term in ("mayorista", "por mayor", "al por mayor")):
        return None

    conversation = Conversation.query.filter_by(
        id=int(conversation_id),
        company_id=int(company_id),
        channel="whatsapp",
    ).first()
    if conversation is None:
        return None

    def quantity_text(value):
        quantity = Decimal(str(value or 0))
        return format(quantity.normalize(), "f").replace(".", ",")

    def quote_wholesale_reply(quote, quote_items):
        wholesale_items = [
            item for item in quote_items
            if str(getattr(item, "price_source", "") or "").strip().lower() == "mayorista"
        ]
        if not wholesale_items:
            return None
        currency = str(getattr(quote, "currency", None) or "ARS").strip().upper() or "ARS"
        list_ids = {int(item.price_list_id) for item in wholesale_items if getattr(item, "price_list_id", None)}
        list_names = []
        for list_id in sorted(list_ids):
            price_list = PriceList.query.filter_by(
                id=list_id,
                company_id=int(company_id),
            ).first()
            if price_list is not None and str(price_list.name or "").strip():
                list_names.append(str(price_list.name).strip())
        lines = [
            f"Sí. El presupuesto *{quote.number or f'P-{quote.id:06d}'}* tiene guardados los precios mayoristas aplicados.",
        ]
        if list_names:
            lines.append(f"Lista de precios: *{', '.join(list_names)}*.")
        for item in quote_items:
            quantity = Decimal(str(item.quantity or 0))
            name = " ".join(str(item.description or "Producto").split())[:180]
            unit_price = _money(item.unit_price)
            subtotal = _money(item.subtotal)
            if str(getattr(item, "price_source", "") or "").strip().lower() == "mayorista":
                lines.append(
                    f"• {quantity_text(quantity)} × {name}: precio mayorista "
                    f"{_format_vendor_currency(unit_price, currency)} c/u; "
                    f"subtotal {_format_vendor_currency(subtotal, currency)}"
                )
                lines.extend(format_vendor_wholesale_price_details(
                    quantity=quantity,
                    unit_price=unit_price,
                    reference_price=getattr(item, "wholesale_reference_price", None),
                    discount_percent=getattr(item, "wholesale_discount_percent", None),
                    currency=currency,
                ))
            else:
                lines.append(
                    f"• {quantity_text(quantity)} × {name}: "
                    f"{_format_vendor_currency(unit_price, currency)} c/u. "
                    "Esta línea no tiene una tarifa mayorista guardada en ese presupuesto."
                )
        lines.extend([
            "",
            f"*Total final guardado del presupuesto:* {_format_vendor_currency(quote.total_amount, currency)}",
            "El descuento indicado ya está reflejado en el precio unitario; no se resta una segunda vez.",
        ])
        return "\n".join(lines)

    state = _metadata(conversation)
    pending_quote_id = state.get(PENDING_QUOTE_KEY)
    if pending_quote_id:
        try:
            quote = Quote.query.filter_by(
                id=int(pending_quote_id),
                company_id=int(company_id),
            ).first()
        except (TypeError, ValueError):
            quote = None
        if quote is not None and quote.status not in {"ANULADO", "RECHAZADO", "VENCIDO", "CONVERTIDO"}:
            quote_items = QuoteItem.query.filter_by(quote_id=quote.id).order_by(
                QuoteItem.sort_order.asc(),
                QuoteItem.id.asc(),
            ).all()
            answer = quote_wholesale_reply(quote, quote_items)
            if answer:
                return answer

    cart = VendorOrderService.get_cart(
        company_id=int(company_id),
        conversation_id=int(conversation_id),
        customer_phone=sender,
    )
    if cart.get("customer_type") == "mayorista":
        price_list = None
        if cart.get("price_list_id"):
            price_list = PriceList.query.filter_by(
                id=int(cart["price_list_id"]),
                company_id=int(company_id),
            ).first()
        if not price_list or not price_list_is_current(price_list):
            list_name = str(getattr(price_list, "name", "") or "").strip()
            detail = f' "{list_name}"' if list_name else ""
            return (
                "Tu ficha figura como mayorista, pero no encuentro una lista de precios "
                f"mayorista{detail} activa y vigente para aplicar. No voy a inventar un descuento. "
                "El comercio debe asignarte o activar una lista vigente."
            )
        if cart.get("items"):
            if cart.get("pricing_valid") is False:
                return (
                    "Tu ficha sí figura como mayorista, pero no puedo confirmar un precio mayorista "
                    f"para todas las líneas del carrito: {cart.get('pricing_error') or 'hay productos sin una tarifa válida para esa cantidad.'}"
                )
            lines = [
                f"Sí. Tu ficha está como mayorista y tiene asignada la lista *{price_list.name}*.",
                "Estos son los precios resueltos para las cantidades actuales:",
            ]
            for item in cart["items"]:
                name = str(item.get("name") or "Producto")
                quantity = Decimal(str(item.get("quantity") or 0))
                unit = _money(item.get("unit_price"))
                subtotal = _money(item.get("subtotal"))
                if str(item.get("price_source") or "").strip().lower() == "mayorista":
                    lines.append(
                        f"• {quantity_text(quantity)} × {name}: precio mayorista "
                        f"{_format_vendor_currency(unit)} c/u; subtotal {_format_vendor_currency(subtotal)}"
                    )
                    lines.extend(format_vendor_wholesale_price_details(
                        quantity=quantity,
                        unit_price=unit,
                        reference_price=item.get("wholesale_reference_price"),
                        discount_percent=item.get("wholesale_discount_percent"),
                    ))
                else:
                    lines.append(
                        f"• {quantity_text(quantity)} × {name}: falta una tarifa mayorista válida "
                        f"para esa cantidad. Motivo: {item.get('price_resolution_reason') or 'sin tarifa aplicable'}."
                    )
            lines.extend([
                "",
                f"*Total de productos:* {_format_vendor_currency(cart.get('total', 0))}",
                "Los descuentos indicados ya están incluidos en los precios unitarios.",
            ])
            return "\n".join(lines)
        return (
            f"Sí. Tu ficha figura como mayorista y tiene asignada la lista *{price_list.name}*. "
            "El descuento depende del producto y de la cantidad. Decime qué producto y cuántas unidades "
            "necesitás, y te confirmo el precio de lista, el porcentaje de descuento y el precio mayorista "
            "que devuelve el sistema."
        )

    return (
        "No quiero darte una respuesta incorrecta: con este número no encuentro una ficha de cliente "
        "configurada como mayorista con una lista vigente asignada. Eso no significa que el comercio "
        "no tenga precios mayoristas. Para calcularlos, el comercio debe marcar tu ficha como mayorista "
        "y asignarle una lista activa; después puedo mostrarte el descuento real por producto y cantidad."
    )


def _handle_vendor_command(company_id: int, conversation_id: int, sender: str, text: str):
    """Deterministic WhatsApp commands keep order and payment actions outside the LLM."""
    normalized = " ".join(text.lower().strip().split())

    if normalized in {"carrito", "ver carrito", "mi carrito", "retomar compra", "continuar compra", "seguir compra", "continuar pedido"}:
        cart = VendorOrderService.get_cart(company_id=company_id, conversation_id=conversation_id)
        if not cart["items"]:
            return "Tu carrito está vacío. Decime qué producto querés agregar."
        lines = [f"• {row['quantity']:g} x {row['name']} — ${row['subtotal']:.2f}" for row in cart["items"]]
        return "🛒 *Tu carrito*\n" + "\n".join(lines) + f"\n\n*Total: ${cart['total']:.2f} ARS*\n\nCuando quieras pagar, escribí *pagar*."

    if normalized == "pedido" or "estado de mi pedido" in normalized or "seguimiento de mi pedido" in normalized or "donde esta mi pedido" in normalized or "dónde está mi pedido" in normalized or "ver mi pedido" in normalized:
        return _customer_order_response(company_id, conversation_id)

    if normalized.startswith("pedido ") or normalized.startswith("estado pedido ") or normalized.startswith("seguimiento "):
        prefix = "pedido " if normalized.startswith("pedido ") else ("estado pedido " if normalized.startswith("estado pedido ") else "seguimiento ")
        return _customer_order_response(company_id, conversation_id, order_number=text[len(prefix):].strip())

    if normalized in {"reintentar pago", "volver a pagar", "nuevo link de pago", "pasame otro link", "pasame el link de pago", "pagar de nuevo"} or ("nuevo link" in normalized and "pago" in normalized):
        result = VendorOrderService.retry_payment(company_id=company_id, conversation_id=conversation_id)
        if result.get("success"):
            return f"Claro. 🔗 Acá tenés el link para pagar nuevamente tu pedido *{result['quote_number']}*:\n{result['payment_url']}"
        messages = {
            "pedido_no_encontrado": "No encuentro un pedido pendiente para volver a pagar.",
            "pedido_ya_confirmado": "Ese pedido ya está confirmado; no necesitás volver a pagar.",
            "pago_ya_aprobado": "El pago de ese pedido ya figura aprobado.",
            "pedido_vencido": "Ese pedido venció y ya no puedo reactivarlo. Podemos preparar uno nuevo.",
            "stock_no_disponible": "El stock actual ya no alcanza para ese pedido. Podemos armar uno nuevo con disponibilidad real.",
            "pedido_no_disponible": "Ese pedido ya no está disponible para volver a pagar.",
        }
        return messages.get(result.get("error"), "No pude generar un nuevo link de pago para ese pedido.")

    if normalized in {"cancelar pedido", "quiero cancelar mi pedido", "anular pedido"} or ("cancelar" in normalized and "pedido" in normalized):
        result = VendorOrderService.cancel_order(company_id=company_id, conversation_id=conversation_id, confirm=False)
        if result.get("confirmation_required"):
            conversation = Conversation.query.filter_by(id=conversation_id, company_id=company_id).first()
            if conversation is not None:
                state = _metadata(conversation)
                state["pending_cancel_quote_number"] = result.get("quote_number") or ""
                state["pending_cancel_created_at"] = utcnow_naive().isoformat()
                _set_metadata(conversation, state)
                db.session.commit()
            return result["message"]
        return "No pude cancelar el pedido."

    if normalized in {"si", "sí", "confirmo", "confirmar", "dale", "ok", "okay", "cancelalo", "cancelálo"}:
        conversation = Conversation.query.filter_by(id=conversation_id, company_id=company_id).first()
        state = _metadata(conversation) if conversation is not None else {}
        pending_number = str(state.pop("pending_cancel_quote_number", "") or "").strip()
        pending_created_at = str(state.pop("pending_cancel_created_at", "") or "").strip()
        if pending_number and pending_created_at:
            try:
                from datetime import datetime, timedelta
                created_at = datetime.fromisoformat(pending_created_at)
                if utcnow_naive() - created_at > timedelta(minutes=10):
                    pending_number = ""
            except (TypeError, ValueError):
                pending_number = ""
        if not pending_number:
            if conversation is not None:
                _set_metadata(conversation, state)
                db.session.commit()
            return None
        if conversation is not None:
            _set_metadata(conversation, state)
        result = VendorOrderService.cancel_order(
            company_id=company_id,
            conversation_id=conversation_id,
            order_number=pending_number,
            confirm=True,
        )
        db.session.commit()
        return result.get("message") if result.get("success") else "No pude cancelar el pedido."

    if normalized in {"catalogo", "catálogo", "ver catalogo", "ver catálogo", "productos"}:
        result = VendorOrderService.list_catalog(company_id=company_id, limit=20)
        if not result.get("products"):
            return "Ahora mismo no encuentro productos activos en el catálogo."
        lines = [f"• {item['name']} — ${item['price']:.2f} ({item['stock']:g} disponibles)" for item in result["products"]]
        return "📦 *Catálogo*\n" + "\n".join(lines)

    if normalized in {"promociones", "ofertas", "promos", "que esta en oferta", "qué está en oferta"}:
        result = VendorOrderService.list_promotions(company_id=company_id)
        lines = [f"• {item['name']} — antes ${item['price']:.2f}, ahora ${item['final_price']:.2f}" for item in result.get("products", []) if item.get("stock", 0) > 0]
        return "🔥 *Promociones*\n" + ("\n".join(lines) if lines else "No hay promociones con stock disponible.")

    if normalized in {"vaciar carrito", "borrar carrito"}:
        VendorOrderService.update_cart(company_id=company_id, conversation_id=conversation_id, clear=True)
        return "Listo, vacié tu carrito."

    if normalized in {"pagar", "quiero pagar", "confirmar pedido", "confirmar compra"}:
        result = VendorOrderService.create_pending_order(company_id=company_id, conversation_id=conversation_id, customer_phone=sender)
        return f"Perfecto. Tu pedido *{result['quote_number']}* suma *${result['total']:.2f} ARS*.\n\nPagalo acá: {result['payment_url']}\n\nUna vez aprobado el pago, StockARmobile confirma la venta y descuenta el stock automáticamente."
    return None


def _assistant_for_inbound(conversation, incoming):
    if incoming is None:
        return None
    query = ConversationMessage.query.filter_by(
        company_id=conversation.company_id,
        conversation_id=conversation.id,
        role="assistant",
    )
    if incoming.trace_id:
        query = query.filter(ConversationMessage.trace_id == incoming.trace_id)
    else:
        query = query.filter(ConversationMessage.id > incoming.id)
    return query.order_by(ConversationMessage.id.desc()).first()


def _send_persisted_reply(company, *, sender: str, assistant) -> bool:
    if assistant is None:
        raise RuntimeError("La respuesta de WhatsApp no está persistida.")
    assistant = db.session.query(ConversationMessage).filter_by(
        id=assistant.id,
        company_id=company.id,
        role="assistant",
    ).with_for_update().first()
    if assistant is None:
        raise RuntimeError("La respuesta de WhatsApp ya no está disponible.")
    metadata = assistant.metadata_json if isinstance(assistant.metadata_json, dict) else {}
    delivery = metadata.get("whatsapp_delivery") if isinstance(metadata.get("whatsapp_delivery"), dict) else {}
    if delivery.get("status") in {"accepted", "blocked_human"}:
        return False

    # Serialize sends against operator handoff. A response generated before
    # takeover must not be sent after the conversation becomes human-owned.
    conversation = (
        db.session.query(Conversation)
        .filter_by(id=assistant.conversation_id, company_id=company.id, channel="whatsapp")
        .with_for_update()
        .first()
    )
    if conversation is None:
        raise RuntimeError("La conversación de WhatsApp ya no está disponible.")
    if attention_for(conversation)["status"] == "human":
        updated_metadata = dict(metadata)
        updated_metadata["whatsapp_delivery"] = {
            "status": "blocked_human",
            "blocked_at": utcnow_naive().isoformat(),
            "reason": "La conversación fue tomada por un operador humano.",
        }
        assistant.metadata_json = updated_metadata
        db.session.commit()
        return False

    result = WhatsAppService.send_text(company, to=sender, body=str(assistant.content or ""))
    messages = result.get("messages") if isinstance(result, dict) else None
    provider_message_id = ""
    if isinstance(messages, list) and messages and isinstance(messages[0], dict):
        provider_message_id = str(messages[0].get("id") or "").strip()
    updated_metadata = dict(metadata)
    updated_metadata["whatsapp_delivery"] = {
        "status": "accepted",
        "accepted_at": utcnow_naive().isoformat(),
        "provider_message_id": provider_message_id,
    }
    assistant.metadata_json = updated_metadata
    db.session.commit()
    return True

def _persist_deterministic_turn(conversation, *, external_id: str, text: str, response: str):
    """Persist a retryable WhatsApp command turn without duplicating messages."""
    incoming = ConversationMessage.query.filter_by(
        company_id=conversation.company_id,
        conversation_id=conversation.id,
        external_message_id=external_id,
        role="user",
    ).first()
    if incoming is None:
        incoming = ConversationMessage(
            conversation_id=conversation.id,
            company_id=conversation.company_id,
            sender_type="user",
            role="user",
            content=text,
            content_type="text",
            external_message_id=external_id,
            idempotency_key=f"whatsapp:{external_id}",
            trace_id=uuid.uuid4().hex,
            metadata_json={"channel": "whatsapp", "deterministic_command": True},
        )
        db.session.add(incoming)
        db.session.flush()
    elif not incoming.trace_id:
        incoming.trace_id = uuid.uuid4().hex

    assistant = _assistant_for_inbound(conversation, incoming)
    if assistant is None:
        assistant = ConversationMessage(
            conversation_id=conversation.id,
            company_id=conversation.company_id,
            sender_type="agent",
            sender_id=conversation.agent_id,
            role="assistant",
            content=response,
            content_type="text",
            trace_id=incoming.trace_id,
            metadata_json={"channel": "whatsapp", "deterministic_command": True, "agent_key": "vendedor"},
        )
        db.session.add(assistant)
    db.session.commit()
    return assistant


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


def _ai_order_conversation_id(payment) -> int | None:
    """Extract the conversation ID from any position in an AI-order reference."""
    external_reference = str(getattr(payment, "external_reference", "") or "")
    match = re.search(r"(?:^|\|)conversation_id:(\d+)(?:\||$)", external_reference)
    return int(match.group(1)) if match else None


def _ai_order_conversation(company_id: int, quote_id: int, payment=None):
    """Resolve the tenant-scoped conversation that originated an AI order."""
    payment = payment or _ai_order_payment(company_id, quote_id)
    conversation_id = _ai_order_conversation_id(payment)
    if conversation_id is None:
        return None
    return Conversation.query.filter_by(
        id=conversation_id,
        company_id=int(company_id),
    ).first()


def _ai_order_channel(company_id: int, quote_id: int, payment=None) -> str:
    """Resolve the real channel that created an AI order."""
    conversation = _ai_order_conversation(company_id, quote_id, payment=payment)
    if conversation is not None:
        return str(conversation.channel or "unknown").strip().lower() or "unknown"
    return "webchat"


def _ai_order_channel_meta(channel: str) -> dict:
    """Return safe presentation metadata for an AI order channel."""
    channel = str(channel or "unknown").strip().lower()
    labels = {
        "webchat": ("Webchat", "bi-globe2"),
        "whatsapp": ("WhatsApp", "bi-whatsapp"),
        "telegram": ("Telegram", "bi-telegram"),
        "instagram": ("Instagram", "bi-instagram"),
        "sms": ("SMS", "bi-chat-dots"),
        "email": ("Email", "bi-envelope"),
    }
    label, icon = labels.get(channel, (channel.replace("_", " ").title() or "Canal", "bi-broadcast"))
    return {"key": channel, "label": label, "icon": icon}


def _ai_order_row(company_id: int, quote):
    payment = _ai_order_payment(company_id, quote.id)
    conversation = _ai_order_conversation(company_id, quote.id, payment=payment)
    payment_status = str(getattr(payment, "status", "pending") or "pending").lower()
    delivery_for_status = getattr(quote, "delivery", None)
    delivery_for_status = getattr(quote, "delivery", None)
    if getattr(quote, "converted_sale_id", None):
        order_key, order_label, order_badge = "confirmed", "Confirmado · venta manual", "text-bg-success"
    elif (
        delivery_for_status is not None
        and getattr(delivery_for_status, "method", "retiro") == "envio"
        and getattr(delivery_for_status, "shipping_status", "confirmed") == "pending"
    ):
        order_key, order_label, order_badge = "shipping_pending", "Esperando cotización de envío", "text-bg-warning"
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
    if (
        getattr(quote, "converted_sale_id", None)
        and payment_status not in {"approved", "paid", "succeeded", "refunded", "charged_back"}
    ):
        # The online checkout is closed when the quote is converted directly from
        # the cart into a sale. Its cancelled payment record must not make the
        # successfully converted order look cancelled in Pedidos IA.
        payment_label, payment_badge = "Cerrado por venta manual", "text-bg-success"
    else:
        payment_label, payment_badge = payment_labels.get(payment_status, (payment_status.replace("_", " ").title(), "text-bg-secondary"))
    client = getattr(quote, "client", None)
    channel = _ai_order_channel(company_id, quote.id, payment)
    channel_meta = _ai_order_channel_meta(channel)
    attention = {}
    if conversation is not None:
        raw_attention = _metadata(conversation).get("ai_attention")
        attention = dict(raw_attention) if isinstance(raw_attention, dict) else {}
    from app import Company
    company = Company.query.filter_by(id=int(company_id)).first()
    vendor_options = get_vendor_options(company)
    attention_status = str(attention.get("status") or "").strip().lower()
    if attention_status == "human":
        attention_key, attention_label, attention_badge = "human", "Atención humana", "text-bg-danger"
    elif attention_status == "resolved":
        attention_key, attention_label, attention_badge = "resolved", "Atención resuelta", "text-bg-success"
    elif vendor_options.get("can_follow_up") and channel == "whatsapp":
        attention_key, attention_label, attention_badge = "followup", "Seguimiento automático", "text-bg-info"
    else:
        attention_key, attention_label, attention_badge = "manual", "Seguimiento manual", "text-bg-secondary"
    return {
        "quote_id": quote.id,
        "conversation_id": conversation.id if conversation is not None else None,
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
        "client_id": getattr(quote, "client_id", None),
        "channel": channel_meta["key"],
        "channel_label": channel_meta["label"],
        "channel_icon": channel_meta["icon"],
        "conversation_id": conversation.id if conversation is not None else None,
        "attention": {
            "key": attention_key,
            "label": attention_label,
            "status": attention_status or "active",
            "reason": str(attention.get("reason") or "").strip(),
            "requested_at": attention.get("requested_at"),
            "resolved_at": attention.get("resolved_at"),
            "auto_followup_enabled": bool(vendor_options.get("can_follow_up")),
            "auto_followup_supported": channel == "whatsapp",
        },
        "delivery": {
            "method": getattr(delivery_for_status, "method", "retiro") if delivery_for_status else "retiro",
            "pickup_status": getattr(delivery_for_status, "pickup_status", None) if delivery_for_status else None,
            "picked_up_at": getattr(delivery_for_status, "picked_up_at", None) if delivery_for_status else None,
            "picked_up_by": getattr(getattr(delivery_for_status, "picked_up_by", None), "name", None) if delivery_for_status else None,
            "recipient_name": getattr(getattr(quote, "delivery", None), "recipient_name", "") if getattr(quote, "delivery", None) else "",
            "phone": getattr(getattr(quote, "delivery", None), "phone", "") if getattr(quote, "delivery", None) else "",
            "address": getattr(getattr(quote, "delivery", None), "address", "") if getattr(quote, "delivery", None) else "",
            "city": getattr(getattr(quote, "delivery", None), "city", "") if getattr(quote, "delivery", None) else "",
            "province": getattr(getattr(quote, "delivery", None), "province", "") if getattr(quote, "delivery", None) else "",
            "postal_code": getattr(getattr(quote, "delivery", None), "postal_code", "") if getattr(quote, "delivery", None) else "",
            "reference": getattr(getattr(quote, "delivery", None), "reference", "") if getattr(quote, "delivery", None) else "",
            "notes": getattr(getattr(quote, "delivery", None), "notes", "") if getattr(quote, "delivery", None) else "",
            "shipping_cost": float(getattr(getattr(quote, "delivery", None), "shipping_cost", 0) or 0) if getattr(quote, "delivery", None) else 0.0,
            "shipping_rate": float(getattr(getattr(quote, "delivery", None), "shipping_rate", 0) or 0) if getattr(quote, "delivery", None) else 0.0,
            "shipping_status": getattr(getattr(quote, "delivery", None), "shipping_status", "confirmed") if getattr(quote, "delivery", None) else "not_required",
            "shipping_source": getattr(getattr(quote, "delivery", None), "shipping_source", "manual") if getattr(quote, "delivery", None) else "none",
            "shipping_confirmed_by_user_id": getattr(getattr(quote, "delivery", None), "shipping_confirmed_by_user_id", None) if getattr(quote, "delivery", None) else None,
            "shipping_confirmed_at": getattr(getattr(quote, "delivery", None), "shipping_confirmed_at", None) if getattr(quote, "delivery", None) else None,
            "pickup_status": getattr(getattr(quote, "delivery", None), "pickup_status", None) if getattr(quote, "delivery", None) else None,
            "picked_up_at": getattr(getattr(quote, "delivery", None), "picked_up_at", None) if getattr(quote, "delivery", None) else None,
            "picked_up_by": getattr(getattr(getattr(quote, "delivery", None), "picked_up_by", None), "name", None) if getattr(quote, "delivery", None) else None,
        },
    }


@bp.get("/pedidos-ia")
@login_required
def ai_orders():
    company_id = get_current_company_id()
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
    allowed_filters = {"pending", "shipping_pending", "paid", "confirmed", "problem", "human", "pickup_pending", "picked_up"}
    rows = [
        row for row in all_rows
        if not selected_status
        or selected_status not in allowed_filters
        or (selected_status == "shipping_pending" and row.get("delivery", {}).get("shipping_status") == "pending")
        or (selected_status == "human" and row.get("attention", {}).get("key") == "human")
        or (selected_status == "pickup_pending" and row.get("delivery", {}).get("method") != "envio" and row.get("sale_id") and not row.get("delivery", {}).get("picked_up_at") and row.get("delivery", {}).get("pickup_status") != "picked_up")
        or (selected_status == "picked_up" and row.get("delivery", {}).get("pickup_status") == "picked_up")
        or (selected_status not in {"shipping_pending", "human", "pickup_pending", "picked_up"} and row["order_key"] == selected_status)
    ]
    channel_keys = []
    for row in all_rows:
        key = row.get("channel") or "unknown"
        if key not in channel_keys:
            channel_keys.append(key)
    channel_labels = [_ai_order_channel_meta(key)["label"] for key in channel_keys]
    summary = {
        "total": len(all_rows),
        "pending": sum(1 for row in all_rows if row["order_key"] == "pending"),
        "shipping_pending": sum(1 for row in all_rows if row.get("delivery", {}).get("shipping_status") == "pending"),
        "paid": sum(1 for row in all_rows if row["payment_status"] == "approved"),
        "confirmed": sum(1 for row in all_rows if row["order_key"] == "confirmed"),
        "pickup_pending": sum(1 for row in all_rows if row.get("delivery", {}).get("method") != "envio" and row.get("sale_id") and not row.get("delivery", {}).get("picked_up_at") and row.get("delivery", {}).get("pickup_status") != "picked_up"),
        "picked_up": sum(1 for row in all_rows if row.get("delivery", {}).get("pickup_status") == "picked_up"),
        "human_attention": sum(1 for row in all_rows if row.get("attention", {}).get("key") == "human"),
        "channel_keys": channel_keys,
        "channel_labels": channel_labels,
        "channel_label": channel_labels[0] if len(channel_labels) == 1 and channel_labels else ("Varios canales" if channel_labels else "Webchat"),
        "channel_icon": _ai_order_channel_meta(channel_keys[0])["icon"] if len(channel_keys) == 1 else "bi-broadcast",
    }
    return render_template("ai_agent/orders.html", orders=rows, summary=summary, selected_status=selected_status if selected_status in allowed_filters else "")


def _tenant_vendor_whatsapp_conversation(company_id: int, conversation_id: int, *, lock: bool = False):
    """Resolve only this tenant's Vendedor IA WhatsApp conversation."""
    agent = choose_agent(int(company_id), channel="whatsapp")
    query = db.session.query(Conversation).filter_by(
        id=int(conversation_id),
        company_id=int(company_id),
        agent_id=int(agent.id),
        channel="whatsapp",
    )
    if lock:
        query = query.with_for_update()
    return query.first()


def _tenant_attention_actor():
    company_id = get_current_company_id()
    if not company_id or getattr(current_user, "role", None) not in {"admin", "user"}:
        abort(403)
    from app import Company
    company = Company.query.filter_by(id=int(company_id), active=True).first()
    if company is None:
        abort(404)
    return int(company_id), company



@bp.post("/pedidos-ia/<int:quote_id>/retirar")
@login_required
def ai_order_mark_picked_up(quote_id: int):
    """Confirm physical pickup on the existing quote delivery, without altering sale/payment."""
    company_id, _company = _tenant_attention_actor()
    from app import Quote, QuoteDelivery
    from stockarmobile.helpers.dates import utcnow_naive

    marker = "Pedido generado por el Vendedor 24 hs de StockARmobile."
    quote = Quote.query.filter_by(
        id=int(quote_id),
        company_id=int(company_id),
        observations=marker,
    ).with_for_update().first()
    if quote is None:
        abort(404)

    delivery = QuoteDelivery.query.filter_by(
        quote_id=int(quote.id),
        company_id=int(company_id),
    ).with_for_update().first()
    if delivery is None or str(delivery.method or "retiro").strip().lower() != "retiro":
        flash("Este pedido no está configurado para retiro en el comercio.", "warning")
        return redirect(url_for("whatsapp_agent.ai_order_detail", quote_id=quote.id))

    if not getattr(quote, "converted_sale_id", None):
        flash("Primero confirmá la venta por el flujo habitual antes de registrar el retiro.", "warning")
        return redirect(url_for("whatsapp_agent.ai_order_detail", quote_id=quote.id))

    current_status = str(getattr(delivery, "pickup_status", None) or "pending").strip().lower()
    if current_status == "picked_up" or getattr(delivery, "picked_up_at", None):
        flash("El retiro ya estaba registrado; no se creó un segundo registro.", "info")
        return redirect(url_for("whatsapp_agent.ai_order_detail", quote_id=quote.id))

    delivery.pickup_status = "picked_up"
    delivery.picked_up_at = utcnow_naive()
    delivery.picked_up_by_user_id = int(current_user.id)
    db.session.commit()
    flash("Retiro registrado con fecha, hora y usuario responsable.", "success")
    return redirect(url_for("whatsapp_agent.ai_order_detail", quote_id=quote.id))


@bp.get("/pedidos-ia/atencion-whatsapp")
@login_required
def ai_whatsapp_attention():
    company_id, company = _tenant_attention_actor()
    agent = choose_agent(company_id, channel="whatsapp")
    query_text = str(request.args.get("q") or "").strip().lower()
    state = str(request.args.get("state") or "all").strip().lower()
    if state not in {"all", "pending", "human", "ai", "resolved"}:
        state = "pending"

    conversations = (
        db.session.query(Conversation)
        .filter_by(company_id=company_id, agent_id=int(agent.id), channel="whatsapp")
        .order_by(Conversation.updated_at.desc(), Conversation.id.desc())
        .limit(200)
        .all()
    )
    all_rows = []
    for conversation in conversations:
        attention = attention_for(conversation)
        latest = (
            ConversationMessage.query
            .filter_by(company_id=company_id, conversation_id=int(conversation.id))
            .order_by(ConversationMessage.id.desc())
            .first()
        )
        phone = str(conversation.external_conversation_id or "").strip()
        latest_text = str(getattr(latest, "content", "") or "")
        if query_text and query_text not in phone.lower() and query_text not in latest_text.lower():
            continue
        all_rows.append({
            "conversation_id": conversation.id,
            "phone": phone or "Sin número",
            "attention": attention,
            "latest_text": latest_text,
            "latest_sender_type": str(getattr(latest, "sender_type", "") or ""),
            "latest_at": getattr(latest, "created_at", None),
            "updated_at": getattr(conversation, "updated_at", None),
        })

    counts = {
        "all": len(all_rows),
        "pending": sum(1 for row in all_rows if row["attention"]["pending"]),
        "human": sum(1 for row in all_rows if row["attention"]["status"] == "human"),
        "ai": sum(1 for row in all_rows if row["attention"]["status"] == "ai"),
        "resolved": sum(1 for row in all_rows if row["attention"]["status"] == "resolved"),
    }
    if state == "pending":
        rows = [row for row in all_rows if row["attention"]["pending"]]
    elif state == "human":
        rows = [row for row in all_rows if row["attention"]["status"] == "human"]
    elif state in {"ai", "resolved"}:
        rows = [row for row in all_rows if row["attention"]["status"] == state]
    else:
        rows = all_rows

    selected = None
    selected_view = None
    selected_id = request.args.get("conversation_id", type=int)
    if selected_id:
        selected = _tenant_vendor_whatsapp_conversation(company_id, selected_id)
    if selected is None:
        preferred = next((row for row in rows if row["attention"]["pending"]), None) or (rows[0] if rows else None)
        if preferred:
            selected = _tenant_vendor_whatsapp_conversation(company_id, preferred["conversation_id"])
    if selected is not None:
        message_rows = (
            ConversationMessage.query
            .filter_by(company_id=company_id, conversation_id=int(selected.id))
            .order_by(ConversationMessage.id.desc())
            .limit(300)
            .all()
        )
        selected_view = {
            "conversation_id": selected.id,
            "phone": str(selected.external_conversation_id or "").strip(),
            "attention": attention_for(selected),
            "updated_at": selected.updated_at,
            "messages": list(reversed(message_rows)),
        }

    return render_template(
        "ai_agent/whatsapp_attention.html",
        company=company,
        rows=rows,
        selected=selected_view,
        q=query_text,
        state=state,
        counts=counts,
        message_limit=300,
    )


@bp.post("/pedidos-ia/atencion-whatsapp/<int:conversation_id>/action")
@login_required
def ai_whatsapp_attention_action(conversation_id: int):
    company_id, _company = _tenant_attention_actor()
    conversation = _tenant_vendor_whatsapp_conversation(company_id, conversation_id, lock=True)
    if conversation is None:
        abort(404)

    action = str(request.form.get("action") or "").strip().lower()
    attention = attention_for(conversation)
    taken_by = attention.get("taken_by_user_id")
    if attention["status"] == "human" and taken_by and int(taken_by) != int(current_user.id):
        flash("La conversación ya está asignada a otro operador.", "warning")
        return redirect(url_for("whatsapp_agent.ai_whatsapp_attention", conversation_id=conversation.id, state="human"))
    if (
        attention["status"] == "human"
        and action in {"resolve", "resume_ai"}
        and int(taken_by or 0) != int(current_user.id)
    ):
        flash("Tomá la conversación antes de cerrar la atención o devolver el control a la IA.", "warning")
        return redirect(url_for("whatsapp_agent.ai_whatsapp_attention", conversation_id=conversation.id, state="human"))

    from app import record_audit
    if action == "take":
        try:
            set_attention(
                conversation,
                status="human",
                user_id=int(current_user.id),
                reason=attention.get("reason") or "Tomada desde Atención WhatsApp de Pedidos IA.",
            )
        except PermissionError:
            db.session.rollback()
            flash("La conversación ya está asignada a otro operador.", "warning")
            return redirect(url_for("whatsapp_agent.ai_whatsapp_attention", conversation_id=conversation.id, state="human"))
        message = "Conversación tomada. El Vendedor IA queda pausado para este cliente."
        audit_action = "vendor_whatsapp_take_human"
    elif action == "resolve":
        if attention["status"] != "human":
            flash("La conversación no está bajo atención humana.", "warning")
            return redirect(url_for("whatsapp_agent.ai_whatsapp_attention", conversation_id=conversation.id))
        set_attention(conversation, status="resolved", user_id=int(current_user.id))
        message = "Atención humana marcada como resuelta."
        audit_action = "vendor_whatsapp_resolve_human"
    elif action == "resume_ai":
        resume_ai(conversation, user_id=int(current_user.id))
        message = "Vendedor IA reanudado para esta conversación."
        audit_action = "vendor_whatsapp_resume_ai"
    else:
        abort(400)

    record_audit(
        action=audit_action,
        entity="conversation",
        entity_id=conversation.id,
        company_id=company_id,
        detail=message,
        user_id=current_user.id,
        ip_address=request.remote_addr,
    )
    db.session.commit()
    return_state = "human" if action != "resume_ai" else "all"
    flash(message, "success")
    return redirect(url_for("whatsapp_agent.ai_whatsapp_attention", conversation_id=conversation.id, state=return_state))


@bp.post("/pedidos-ia/atencion-whatsapp/<int:conversation_id>/mensaje")
@login_required
def ai_whatsapp_attention_message(conversation_id: int):
    company_id, company = _tenant_attention_actor()
    conversation = _tenant_vendor_whatsapp_conversation(company_id, conversation_id, lock=True)
    if conversation is None:
        abort(404)

    attention = attention_for(conversation)
    if attention["status"] != "human":
        flash("Tomá la conversación antes de enviar un mensaje humano.", "warning")
        return redirect(url_for("whatsapp_agent.ai_whatsapp_attention", conversation_id=conversation.id))
    if int(attention.get("taken_by_user_id") or 0) != int(current_user.id):
        flash("Tomá la conversación antes de responder. Si ya está asignada, debe responder el operador responsable.", "warning")
        return redirect(url_for("whatsapp_agent.ai_whatsapp_attention", conversation_id=conversation.id, state="human"))

    body = str(request.form.get("body") or "").strip()
    recipient = str(conversation.external_conversation_id or "").strip()
    recipient_digits = "".join(character for character in recipient if character.isdigit())
    if not body:
        flash("Escribí un mensaje antes de enviarlo.", "warning")
        return redirect(url_for("whatsapp_agent.ai_whatsapp_attention", conversation_id=conversation.id))
    if len(body) > 4096:
        flash("El mensaje no puede superar los 4096 caracteres.", "warning")
        return redirect(url_for("whatsapp_agent.ai_whatsapp_attention", conversation_id=conversation.id))
    if not recipient_digits:
        flash("La conversación no tiene un número de WhatsApp válido.", "danger")
        return redirect(url_for("whatsapp_agent.ai_whatsapp_attention", conversation_id=conversation.id))

    connection = get_whatsapp_connection(company)
    if not connection.get("enabled") or not connection.get("phone_number_id") or not connection.get("access_token"):
        flash("WhatsApp no está conectado o habilitado para este comercio.", "danger")
        return redirect(url_for("whatsapp_agent.ai_whatsapp_attention", conversation_id=conversation.id))

    try:
        result = WhatsAppService.send_text(company, to=recipient, body=body)
        messages = result.get("messages") if isinstance(result, dict) else None
        provider_message_id = ""
        if isinstance(messages, list) and messages and isinstance(messages[0], dict):
            provider_message_id = str(messages[0].get("id") or "").strip()

        now = utcnow_naive()
        outgoing = ConversationMessage(
            company_id=company_id,
            conversation_id=conversation.id,
            sender_type="human",
            sender_id=int(current_user.id),
            role="assistant",
            content=body,
            content_type="text",
            external_message_id=(f"human:{provider_message_id}" if provider_message_id else f"human:{uuid.uuid4().hex}")[:255],
            metadata_json={
                "channel": "whatsapp",
                "human_operator": True,
                "human_operator_user_id": int(current_user.id),
                "whatsapp_delivery": {
                    "status": "accepted",
                    "accepted_at": now.isoformat(),
                    "provider_message_id": provider_message_id,
                },
            },
        )
        db.session.add(outgoing)
        metadata = conversation_metadata(conversation)
        current_attention = metadata.get("ai_attention")
        current_attention = dict(current_attention) if isinstance(current_attention, dict) else {}
        current_attention.update({
            "status": "human",
            "taken_by_user_id": int(current_user.id),
            "taken_at": current_attention.get("taken_at") or now.isoformat(),
            "last_human_message_at": now.isoformat(),
        })
        metadata["ai_attention"] = current_attention
        conversation.metadata_json = metadata
        conversation.updated_at = now

        from app import record_audit
        record_audit(
            action="vendor_whatsapp_human_message",
            entity="conversation",
            entity_id=conversation.id,
            company_id=company_id,
            detail="Mensaje humano enviado por el panel de Atención WhatsApp.",
            user_id=current_user.id,
            ip_address=request.remote_addr,
        )
        db.session.commit()
        flash("Mensaje aceptado por WhatsApp y registrado en la conversación.", "success")
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "No se pudo enviar mensaje humano por WhatsApp company_id=%s conversation_id=%s user_id=%s",
            company_id,
            conversation_id,
            current_user.id,
        )
        flash("No se pudo enviar el mensaje. Revisá la conexión de WhatsApp y volvé a intentar.", "danger")
    return redirect(url_for("whatsapp_agent.ai_whatsapp_attention", conversation_id=conversation_id, state="human"))


@bp.post("/pedidos-ia/<int:quote_id>/shipping")
@company_admin_required
def ai_order_shipping(quote_id: int):
    company_id = get_current_company_id()
    if not company_id:
        abort(403)
    raw_cost = str(request.form.get("shipping_cost") or "").strip().replace(",", ".")
    try:
        from decimal import Decimal, InvalidOperation
        cost = Decimal(raw_cost)
    except (InvalidOperation, ValueError):
        flash("Ingresá un costo de envío válido.", "danger")
        return redirect(url_for("whatsapp_agent.ai_order_detail", quote_id=quote_id))
    try:
        result = VendorOrderService.set_shipping_cost_and_generate_payment(
            company_id=int(company_id),
            quote_id=int(quote_id),
            shipping_cost=cost,
            user_id=int(current_user.id),
        )
        conversation_id = VendorOrderService._conversation_id_for_quote(
            company_id=int(company_id),
            quote_id=int(quote_id),
        )
        if conversation_id and result.get("payment_url"):
            conversation = Conversation.query.filter_by(
                id=int(conversation_id),
                company_id=int(company_id),
            ).first()
            if conversation is not None and str(conversation.channel or "").lower() == "whatsapp":
                total_text = "{:.2f}".format(float(result.get("total") or 0))
                body = (
                    "✅ Tu pedido "
                    + str(result.get("quote_number") or "IA")
                    + " ya tiene el envío confirmado. Total final: $"
                    + total_text
                    + " ARS. Pagalo acá: "
                    + str(result.get("payment_url") or "")
                )
                try:
                    WhatsAppService.send_text(
                        conversation.company,
                        to=str(conversation.external_conversation_id or "").strip(),
                        body=body,
                    )
                except Exception:
                    current_app.logger.exception(
                        "No se pudo enviar automáticamente el link de pago por WhatsApp quote_id=%s",
                        quote_id,
                    )
        flash("Costo de envío confirmado. El enlace de cobro quedó generado.", "success")
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    except Exception:
        db.session.rollback()
        current_app.logger.exception("No se pudo confirmar el costo de envío quote_id=%s", quote_id)
        flash("No se pudo confirmar el costo de envío. Revisá el pedido y volvé a intentar.", "danger")
    return redirect(url_for("whatsapp_agent.ai_order_detail", quote_id=quote_id))


@bp.post("/pedidos-ia/<int:quote_id>/attention")
@company_admin_required
def ai_order_attention(quote_id: int):
    company_id = get_current_company_id()
    if not company_id:
        abort(403)
    from app import Quote, record_audit

    marker = "Pedido generado por el Vendedor 24 hs de StockARmobile."
    quote = Quote.query.filter_by(
        id=int(quote_id),
        company_id=int(company_id),
        observations=marker,
    ).first()
    if quote is None:
        abort(404)

    conversation = _ai_order_conversation(int(company_id), int(quote.id))
    if conversation is None:
        flash("No encuentro la conversación asociada a este pedido.", "warning")
        return redirect(url_for("whatsapp_agent.ai_order_detail", quote_id=quote.id))

    action = str(request.form.get("action") or "").strip().lower()
    metadata = _metadata(conversation)
    current = metadata.get("ai_attention")
    current = dict(current) if isinstance(current, dict) else {}
    now = utcnow_naive().isoformat()

    if action == "request_human":
        reason = str(request.form.get("reason") or "").strip()[:500]
        metadata["ai_attention"] = {
            "status": "human",
            "reason": reason or "Atención solicitada desde Pedidos IA.",
            "requested_at": now,
            "requested_by_user_id": int(current_user.id),
        }
        for item in metadata.get("ai_outbox") or []:
            if isinstance(item, dict) and str(item.get("status") or "pending") == "pending":
                item["status"] = "blocked"
                item["blocked_at"] = now
                item["error"] = "Atención humana solicitada."
        flash("Pedido marcado para atención humana.", "success")
    elif action == "resolve":
        metadata["ai_attention"] = {
            **current,
            "status": "resolved",
            "resolved_at": now,
            "resolved_by_user_id": int(current_user.id),
        }
        for item in metadata.get("ai_outbox") or []:
            if isinstance(item, dict) and str(item.get("status") or "pending") == "pending":
                item["status"] = "blocked"
                item["blocked_at"] = now
                item["error"] = "Atención humana cerrada; reanudación IA pendiente de acción."
        flash("Atención humana marcada como resuelta.", "success")
    elif action == "resume_ai":
        metadata.pop("ai_attention", None)
        flash("Seguimiento IA restablecido para esta conversación.", "success")
    else:
        abort(400)

    _set_metadata(conversation, metadata)
    record_audit(
        action=f"ai_order_attention_{action}",
        entity="quote",
        entity_id=quote.id,
        detail=f"Atención de pedido IA: {action}",
        ip_address=request.remote_addr,
    )
    db.session.commit()
    return redirect(url_for("whatsapp_agent.ai_order_detail", quote_id=quote.id))


@bp.get("/pedidos-ia/<int:quote_id>")
@login_required
def ai_order_detail(quote_id: int):
    company_id = get_current_company_id()
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
        items=list(getattr(quote, "items", []) or []),
        payment=_ai_order_payment(company_id, quote.id),
    )


@bp.route("/api/whatsapp/webhook", methods=["GET", "POST"])
@csrf.exempt
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
    retryable_errors = False
    try:
        _record_commercial_campaign_status(_extract_statuses(payload))
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Commercial WhatsApp campaign status processing failed")
        errors.append({"error": "commercial_campaign_status_internal_error"})
        retryable_errors = True

    for phone_number_id, sender, external_id, text in _extract_messages(payload):
        try:
            if is_commercial_phone_number_id(phone_number_id):
                try:
                    commercial_result = process_commercial_message(
                        phone_number_id=phone_number_id,
                        sender=sender,
                        external_id=external_id,
                        text=text,
                    )
                    if commercial_result.get("status") in {"completed", "duplicate"}:
                        processed += 1
                except Exception:
                    db.session.rollback()
                    current_app.logger.exception(
                        "Commercial WhatsApp agent error external_message_id=%s",
                        external_id,
                    )
                    errors.append({"external_message_id": external_id, "error": "commercial_internal_error"})
                    retryable_errors = True
                continue

            company = company_for_whatsapp_phone_id(phone_number_id)
            if company is None:
                errors.append({"external_message_id": external_id, "error": "company_not_configured"})
                continue

            connection = get_whatsapp_connection(company)
            if not connection["enabled"]:
                continue

            conversation = _get_or_create_conversation(company.id, sender)
            duplicate = db.session.query(ConversationMessage).filter(
                ConversationMessage.company_id == company.id,
                ConversationMessage.conversation_id == conversation.id,
                ConversationMessage.external_message_id == external_id,
                ConversationMessage.role == "user",
            ).first()
            attention = attention_for(conversation)

            # Human-owned conversations persist inbound messages but never invoke
            # deterministic vendor commands or the AI runtime.
            if attention["status"] == "human":
                persist_human_inbound(
                    conversation,
                    external_id=external_id,
                    sender=sender,
                    text=text,
                )
                db.session.commit()
                processed += 1
                continue

            # Do not turn a Meta webhook retry for a human-owned message into an
            # AI reply after the operator later resumes the conversation.
            duplicate_metadata = (
                duplicate.metadata_json
                if duplicate is not None and isinstance(duplicate.metadata_json, dict)
                else {}
            )
            if duplicate is not None and duplicate_metadata.get("human_attention"):
                continue

            # Resolution returns control to the AI on the next new customer
            # message; blocked reminders are deliberately not resurrected.
            if attention["status"] == "resolved":
                resume_ai(conversation)
                db.session.commit()

            if not is_ai_enabled(company):
                continue

            if duplicate is not None:
                assistant_duplicate = _assistant_for_inbound(conversation, duplicate)
                if assistant_duplicate is not None:
                    if not _send_persisted_reply(company, sender=sender, assistant=assistant_duplicate):
                        continue
                    processed += 1
                    continue

            access = can_use_ai(company, "vendedor")
            if not access.allowed:
                errors.append({"external_message_id": external_id, "error": "ai_plan_blocked", "reason": access.reason})
                continue
            if len(text) > MAX_AGENT_MESSAGE_CHARS:
                errors.append({"external_message_id": external_id, "error": "message_too_long"})
                continue

            command_response = _handle_vendor_command(company.id, conversation.id, sender, text)
            if command_response is None:
                command_response = _handle_vendor_wholesale_inquiry(
                    company.id,
                    conversation.id,
                    sender,
                    text,
                )
            if command_response is not None:
                assistant = _persist_deterministic_turn(
                    conversation,
                    external_id=external_id,
                    text=text,
                    response=command_response,
                )
                if _send_persisted_reply(company, sender=sender, assistant=assistant):
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

            if result.get("status") == "disabled":
                continue
            assistant_id = result.get("assistant_message_id")
            assistant = db.session.query(ConversationMessage).filter_by(
                id=assistant_id,
                company_id=company.id,
                conversation_id=conversation.id,
                role="assistant",
            ).first() if assistant_id else _assistant_for_inbound(
                conversation,
                db.session.query(ConversationMessage).filter_by(
                    company_id=company.id,
                    conversation_id=conversation.id,
                    external_message_id=external_id,
                    role="user",
                ).first(),
            )
            if _send_persisted_reply(company, sender=sender, assistant=assistant):
                processed += 1
        except Exception:
            db.session.rollback()
            current_app.logger.exception("WhatsApp agent error external_message_id=%s", external_id)
            errors.append({"external_message_id": external_id, "error": "internal_error"})
            retryable_errors = True
    return jsonify({"success": not retryable_errors, "processed": processed, "errors": errors}), 503 if retryable_errors else 200
