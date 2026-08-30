"""Atomic payment-to-sale finalization for AI vendor orders."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from decimal import Decimal
from typing import Any

import requests
from sqlalchemy import event
from sqlalchemy.orm import Session

from services.ai_agent.config_service import get_whatsapp_connection


LOGGER = logging.getLogger(__name__)
FLOW_PREFIX = "flow:ai_order"
_FINALIZE_QUEUE_KEY = "stockarmobile_ai_order_finalize"
_AFTER_COMMIT_FLAG = "stockarmobile_ai_order_after_commit_running"


def _reference_parts(external_reference: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for segment in str(external_reference or "").split("|"):
        if ":" not in segment:
            continue
        key, value = segment.split(":", 1)
        result[key] = value
    return result


def _money(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))


def _format_ars(value: Any) -> str:
    amount = _money(value)
    raw = f"{amount:,.2f}"
    return raw.replace(",", "X").replace(".", ",").replace("X", ".")


def _parse_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _metadata(value: Any) -> dict[str, Any]:
    parsed = _parse_payload(value)
    return dict(parsed)


def _payment_candidates(session: Session):
    from app import Payment

    seen: set[int] = set()
    for obj in list(session.new) + list(session.dirty):
        if not isinstance(obj, Payment):
            continue
        marker = id(obj)
        if marker in seen:
            continue
        seen.add(marker)
        yield obj


def _queue_candidate(session: Session, payment) -> None:
    if str(getattr(payment, "status", "") or "").strip().lower() != "approved":
        return
    external_reference = str(getattr(payment, "external_reference", "") or "").strip()
    if not external_reference.startswith(FLOW_PREFIX + "|"):
        return

    parts = _reference_parts(external_reference)
    if str(parts.get("quote_id") or "").isdigit() is False:
        return
    if str(parts.get("company_id") or "").isdigit() is False:
        return

    key = str(getattr(payment, "payment_id", None) or external_reference)
    queued = session.info.setdefault(_FINALIZE_QUEUE_KEY, {})
    if key in queued:
        return
    queued[key] = {
        "company_id": int(parts["company_id"]),
        "quote_id": int(parts["quote_id"]),
        "conversation_id": int(parts["conversation_id"]) if str(parts.get("conversation_id") or "").isdigit() else None,
        "payment_id": str(getattr(payment, "payment_id", "") or "").strip(),
        "payment_payload": _parse_payload(getattr(payment, "payload_json", None)),
        "external_reference": external_reference,
    }


def _before_commit(session: Session) -> None:
    if session.info.get(_AFTER_COMMIT_FLAG):
        return
    for payment in _payment_candidates(session):
        _queue_candidate(session, payment)


def _find_or_create_sale(session: Session, *, company_id: int, quote, payment) -> tuple[Any, bool]:
    from app import Product, Sale, SaleItem

    if quote.converted_sale_id:
        sale = session.query(Sale).filter_by(id=quote.converted_sale_id, company_id=company_id).first()
        return sale, False

    existing_sale = session.query(Sale).filter_by(
        client_txn_id=f"ai-order-quote:{quote.id}",
        company_id=company_id,
    ).first()
    if existing_sale is not None:
        quote.converted_sale_id = existing_sale.id
        quote.status = "CONVERTIDO"
        return existing_sale, False

    item_rows = list(quote.items)
    if not item_rows:
        raise ValueError("El pedido no tiene líneas.")

    product_ids = sorted({int(item.product_id) for item in item_rows if item.product_id})
    products = {
        row.id: row
        for row in session.query(Product)
        .filter(Product.company_id == company_id, Product.id.in_(product_ids), Product.active.is_(True))
        .with_for_update()
        .all()
    }

    expected_total = _money(quote.total_amount)
    payment_amount = _money(getattr(payment, "amount", 0))
    if expected_total != payment_amount:
        raise ValueError(
            f"El importe aprobado ({payment_amount}) no coincide con el pedido ({expected_total})."
        )
    if str(getattr(payment, "currency", "ARS") or "ARS").upper() != str(quote.currency or "ARS").upper():
        raise ValueError("La moneda del pago no coincide con el pedido.")

    lines = []
    for item in item_rows:
        product = products.get(int(item.product_id or 0))
        if product is None:
            raise ValueError("Uno de los productos del pedido ya no existe.")
        quantity = Decimal(str(item.quantity or 0))
        if quantity <= 0:
            raise ValueError("El pedido contiene una cantidad inválida.")
        available = Decimal(str(product.stock or 0))
        if available < quantity:
            raise ValueError(
                f"Stock insuficiente para completar {product.name}. Disponible: {available:g}; solicitado: {quantity:g}."
            )
        lines.append((item, product, quantity))

    sale = Sale(
        date=datetime.utcnow(),
        customer=quote.client.name if quote.client is not None else (quote.consumer_name or "Consumidor final"),
        subtotal=_money(quote.subtotal),
        discount=_money(quote.discount),
        tax=_money(quote.tax),
        total_amount=expected_total,
        payment_method="MERCADO_PAGO",
        secondary_payment_method=None,
        paid_amount=expected_total,
        secondary_paid_amount=Decimal("0.00"),
        surcharge=_money(quote.surcharge),
        discount_type=quote.discount_type,
        discount_value=quote.discount_value,
        discount_reason=quote.discount_reason,
        surcharge_type=quote.surcharge_type,
        surcharge_value=quote.surcharge_value,
        surcharge_reason=quote.surcharge_reason,
        client_txn_id=f"ai-order-quote:{quote.id}",
        document_type="venta",
        requiere_comprobante=False,
        comprobante_emitido=False,
        status="confirmada",
        note=f"Venta generada automáticamente por Vendedor 24 hs. Presupuesto {quote.number or quote.id}.",
        client_id=quote.client_id,
        seller_id=quote.seller_id or quote.created_by_user_id,
        company_id=company_id,
        cash_session_id=None,
    )
    session.add(sale)
    session.flush()

    for item, product, quantity in lines:
        product.stock = float(product.stock or 0) - float(quantity)
        session.add(
            SaleItem(
                sale_id=sale.id,
                product_id=product.id,
                quantity=float(quantity),
                price=_money(item.unit_price),
                cost_price=_money(product.cost_price),
                discount=_money(item.discount),
            )
        )

    quote.status = "CONVERTIDO"
    quote.converted_sale_id = sale.id
    payment.status = "approved"
    payment.paid_at = payment.paid_at or datetime.utcnow()
    return sale, True


def _finalize_one(session: Session, item: dict[str, Any]) -> dict[str, Any] | None:
    from app import Conversation, ConversationMessage, Payment, Quote

    company_id = int(item["company_id"])
    quote_id = int(item["quote_id"])
    payment_id = str(item.get("payment_id") or "").strip()
    quote = session.query(Quote).filter_by(id=quote_id, company_id=company_id).first()
    if quote is None:
        raise ValueError("Presupuesto del pedido no encontrado para esta empresa.")

    payment = None
    if payment_id:
        payment = session.query(Payment).filter_by(payment_id=payment_id, company_id=company_id).first()
    if payment is None:
        payment = session.query(Payment).filter_by(
            external_reference=item["external_reference"],
            company_id=company_id,
        ).order_by(Payment.id.desc()).first()
    if payment is None:
        raise ValueError("No se encontró el pago aprobado asociado al pedido.")

    if quote.converted_sale_id:
        sale_id = quote.converted_sale_id
        sale_number = quote.number or f"P-{quote.id:06d}"
        total = _money(quote.total_amount)
    else:
        sale, _created = _find_or_create_sale(
            session,
            company_id=company_id,
            quote=quote,
            payment=payment,
        )
        sale_id = sale.id
        sale_number = quote.number or f"P-{quote.id:06d}"
        total = _money(quote.total_amount)

    conversation = None
    if item.get("conversation_id"):
        conversation = session.query(Conversation).filter_by(
            id=int(item["conversation_id"]),
            company_id=company_id,
        ).first()

    confirmation = (
        f"✅ Pago confirmado.\n"
        f"Pedido {sale_number} aprobado por ${_format_ars(total)} ARS.\n"
        f"Venta registrada: #{sale_id}.\n"
        f"Gracias por tu compra."
    )
    whatsapp_to = None
    whatsapp_connection = None
    if conversation is not None:
        state = _metadata(conversation.metadata_json)
        state["vendor_cart"] = {}
        state.pop("pending_quote_id", None)
        state.pop("pending_payment_url", None)
        state["last_paid_quote_id"] = quote.id
        state["last_sale_id"] = sale_id
        conversation.metadata_json = state
        whatsapp_to = str(conversation.external_conversation_id or "").strip() or None
        if whatsapp_to and conversation.channel == "whatsapp":
            company = session.get(__import__("app").Company, company_id)
            if company is not None:
                whatsapp_connection = get_whatsapp_connection(company)

        if whatsapp_to:
            key = f"ai-order-paid:{payment.payment_id or payment.id}"
            exists = session.query(ConversationMessage).filter_by(
                company_id=company_id,
                idempotency_key=key,
            ).first()
            if exists is None:
                session.add(
                    ConversationMessage(
                        conversation_id=conversation.id,
                        company_id=company_id,
                        sender_type="agent",
                        sender_id=conversation.agent_id,
                        role="assistant",
                        content=confirmation,
                        content_type="text",
                        external_message_id=None,
                        idempotency_key=key,
                        trace_id=f"ai-order-paid:{payment.payment_id or payment.id}",
                        metadata_json={"event": "ai_order_paid", "sale_id": sale_id, "quote_id": quote.id},
                    )
                )

    if whatsapp_to and whatsapp_connection and whatsapp_connection.get("enabled") and whatsapp_connection.get("phone_number_id") and whatsapp_connection.get("access_token"):
        session.info.setdefault(_FINALIZE_QUEUE_KEY + "_send", []).append({
            "company_id": company_id,
            "to": whatsapp_to,
            "body": confirmation,
            "phone_number_id": whatsapp_connection["phone_number_id"],
            "access_token": whatsapp_connection["access_token"],
        })

    return {
        "company_id": company_id,
        "quote_id": quote.id,
        "sale_id": sale_id,
        "payment_id": payment.payment_id or str(payment.id),
        "total": float(total),
    }


def _after_commit(session: Session) -> None:
    queue = session.info.pop(_FINALIZE_QUEUE_KEY, None)
    if not queue:
        return
    if session.info.get(_AFTER_COMMIT_FLAG):
        return

    session.info[_AFTER_COMMIT_FLAG] = True
    try:
        # The original transaction is already committed here, so use a fresh
        # transaction on the same Session. This keeps the existing Flask DB
        # binding while avoiding writes inside the after_commit callback itself.
        session.begin()
        results = []
        for item in queue.values():
            try:
                results.append(_finalize_one(session, item))
            except Exception:
                LOGGER.exception(
                    "AI order finalization failed: company_id=%s quote_id=%s payment_id=%s",
                    item.get("company_id"), item.get("quote_id"), item.get("payment_id"),
                )
                session.rollback()
                raise
        # Commit the sale/stock/conversation transaction. The flag prevents
        # our own approval Payment updates from being re-queued recursively.
        session.commit()
        _send_queued_whatsapp(session.info.pop(_FINALIZE_QUEUE_KEY + "_send", []))
        if results:
            LOGGER.info("AI order finalization completed: count=%s", len(results))
    finally:
        session.info.pop(_AFTER_COMMIT_FLAG, None)


def _send_queued_whatsapp(queue: list[dict[str, Any]]) -> None:
    for item in queue:
        try:
            recipient = "".join(ch for ch in str(item.get("to") or "") if ch.isdigit())
            if not recipient:
                continue
            url = f"https://graph.facebook.com/{__import__('os').getenv('WHATSAPP_GRAPH_API_VERSION', 'v23.0')}/{item['phone_number_id']}/messages"
            response = requests.post(
                url,
                json={
                    "messaging_product": "whatsapp",
                    "to": recipient,
                    "type": "text",
                    "text": {"preview_url": True, "body": str(item.get("body") or "")[:4096]},
                },
                headers={
                    "Authorization": f"Bearer {item['access_token']}",
                    "Content-Type": "application/json",
                },
                timeout=float(__import__('os').getenv("WHATSAPP_API_TIMEOUT", "20")),
            )
            if response.status_code >= 400:
                raise RuntimeError(f"WhatsApp API HTTP {response.status_code}: {response.text[:500]}")
        except Exception:
            LOGGER.exception(
                "WhatsApp confirmation failed after AI order payment: company_id=%s to=%s",
                item.get("company_id"), item.get("to"),
            )


# Register once when the application imports the models package. Callbacks use
# lazy imports so this module can load before app.py has declared its models.
event.listen(Session, "before_commit", _before_commit, propagate=True)
event.listen(Session, "after_commit", _after_commit, propagate=True)
