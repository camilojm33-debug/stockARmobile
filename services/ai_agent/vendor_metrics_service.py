"""Tenant-scoped metrics and attribution for the public Vendor IA."""
from __future__ import annotations

import json
import re
from collections import defaultdict
from decimal import Decimal
from typing import Any

from flask import request, session

from stockarmobile.extensions import db
from stockarmobile.models.conversations import Agent, Conversation


METRICS_KEY = "vendor_metrics"
_MAX_PRODUCT_IDS = 500
_MAX_VISITOR_EVENTS = 200


def _metadata(value: Any) -> dict:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _metric_state(conversation) -> dict:
    metadata = _metadata(conversation.metadata_json)
    state = metadata.get(METRICS_KEY)
    if not isinstance(state, dict):
        state = {}
    counts = state.get("counts")
    state["counts"] = counts if isinstance(counts, dict) else {}
    products = state.get("products")
    state["products"] = products if isinstance(products, dict) else {}
    seen = state.get("seen_events")
    state["seen_events"] = seen if isinstance(seen, list) else []
    metadata[METRICS_KEY] = state
    conversation.metadata_json = metadata
    return state


def record_vendor_event(
    conversation,
    event: str,
    *,
    product_ids: list[int] | None = None,
    unique_key: str | None = None,
) -> bool:
    """Record a public Vendor event without creating a second source of truth."""
    if conversation is None or not getattr(conversation, "id", None):
        return False
    event = str(event or "").strip().lower()
    if not event:
        return False

    state = _metric_state(conversation)
    seen = state["seen_events"]
    unique_key = str(unique_key or "").strip()
    if unique_key and unique_key in seen:
        return False
    if unique_key:
        seen.append(unique_key)
        state["seen_events"] = seen[-_MAX_VISITOR_EVENTS:]

    counts = state["counts"]
    counts[event] = int(counts.get(event, 0) or 0) + 1
    if product_ids:
        products = state["products"]
        for raw_id in product_ids:
            try:
                product_id = str(int(raw_id))
            except (TypeError, ValueError):
                continue
            products[product_id] = int(products.get(product_id, 0) or 0) + 1
        state["products"] = dict(list(products.items())[-_MAX_PRODUCT_IDS:])

    metadata = _metadata(conversation.metadata_json)
    metadata[METRICS_KEY] = state
    conversation.metadata_json = metadata
    db.session.flush()
    return True


def _reference_value(reference: str, key: str) -> int | None:
    match = re.search(rf"(?:^|\|){re.escape(key)}:(\d+)(?:\||$)", str(reference or ""))
    return int(match.group(1)) if match else None


def _public_conversations(company_id: int):
    from services.ai_agent.config_service import VENDOR_AGENT_NAME

    return (
        Conversation.query
        .join(Agent, Agent.id == Conversation.agent_id)
        .filter(
            Conversation.company_id == int(company_id),
            Agent.name == VENDOR_AGENT_NAME,
            Conversation.channel.in_(["webchat", "whatsapp"]),
        )
    )


def _conversation_metric_totals(rows) -> dict:
    visits = 0
    conversations_started = 0
    product_consultations = 0
    cart_actions = 0
    product_frequency: dict[str, int] = defaultdict(int)
    active_carts = 0
    channels: dict[str, dict[str, int]] = defaultdict(lambda: {"visits": 0, "conversations": 0, "cart_actions": 0})

    for conversation in rows:
        state = _metadata(conversation.metadata_json).get(METRICS_KEY) or {}
        if not isinstance(state, dict):
            state = {}
        counts = state.get("counts") if isinstance(state.get("counts"), dict) else {}
        products = state.get("products") if isinstance(state.get("products"), dict) else {}
        visits += int(counts.get("visit", 0) or 0)
        conversations_started += int(counts.get("conversation_started", 0) or 0)
        product_consultations += int(counts.get("product_query", 0) or 0)
        cart_actions += int(counts.get("cart_add", 0) or 0) + int(counts.get("cart_remove", 0) or 0)
        for product_id, total in products.items():
            product_frequency[str(product_id)] += int(total or 0)
        metadata = _metadata(conversation.metadata_json)
        raw_cart = metadata.get("vendor_cart")
        if isinstance(raw_cart, dict) and raw_cart:
            active_carts += 1
        channel = str(conversation.channel or "unknown")
        channels[channel]["visits"] += int(counts.get("visit", 0) or 0)
        channels[channel]["conversations"] += int(counts.get("conversation_started", 0) or 0)
        channels[channel]["cart_actions"] += int(counts.get("cart_add", 0) or 0) + int(counts.get("cart_remove", 0) or 0)

    return {
        "visits": visits,
        "conversations": conversations_started,
        "product_consultations": product_consultations,
        "cart_actions": cart_actions,
        "active_carts": active_carts,
        "product_frequency": dict(sorted(product_frequency.items(), key=lambda item: item[1], reverse=True)[:20]),
        "channels": dict(channels),
    }


def build_vendor_metrics(*, company_id: int) -> dict:
    """Build cumulative, tenant-scoped Vendor IA metrics."""
    company_id = int(company_id)
    rows = _public_conversations(company_id).all()
    base = _conversation_metric_totals(rows)

    from app import Payment, Quote, Sale

    payments = Payment.query.filter_by(company_id=company_id, provider="mercadopago_ai_order").all()
    quote_ids: set[int] = set()
    paid_quote_ids: set[int] = set()
    quote_conversations: dict[int, int] = {}
    payment_attempts = 0
    approved_payments = 0

    for payment in payments:
        reference = str(getattr(payment, "external_reference", "") or "")
        quote_id = _reference_value(reference, "quote_id")
        conversation_id = _reference_value(reference, "conversation_id")
        if not quote_id:
            continue
        quote_ids.add(quote_id)
        if conversation_id:
            quote_conversations[quote_id] = conversation_id
        payment_attempts += 1
        if str(getattr(payment, "status", "") or "").lower() == "approved":
            approved_payments += 1
            paid_quote_ids.add(quote_id)

    order_count = len(quote_ids)
    paid_orders = len(paid_quote_ids)
    sales_amount = Decimal("0.00")
    sales_count = 0
    quote_to_sale: dict[int, Any] = {}
    if quote_ids:
        sales = (
            db.session.query(Sale, Quote.id)
            .join(Quote, Quote.converted_sale_id == Sale.id)
            .filter(
                Sale.company_id == company_id,
                Quote.id.in_(list(quote_ids)),
            )
            .all()
        )
        for sale, quote_id in sales:
            if int(quote_id) in quote_to_sale:
                continue
            quote_to_sale[int(quote_id)] = sale
            sales_count += 1
            sales_amount += Decimal(str(getattr(sale, "total_amount", 0) or 0))

    channel_conversion = defaultdict(lambda: {"orders": 0, "paid_orders": 0, "sales": 0, "amount": Decimal("0.00")})
    for quote_id in quote_ids:
        conversation_id = quote_conversations.get(quote_id)
        if not conversation_id:
            continue
        conversation = (
            Conversation.query
            .filter_by(id=int(conversation_id), company_id=company_id)
            .first()
        )
        channel = str(getattr(conversation, "channel", None) or "unknown")
        channel_conversion[channel]["orders"] += 1
        if quote_id in paid_quote_ids:
            channel_conversion[channel]["paid_orders"] += 1
        sale = quote_to_sale.get(quote_id)
        if sale is not None:
            channel_conversion[channel]["sales"] += 1
            channel_conversion[channel]["amount"] += Decimal(str(getattr(sale, "total_amount", 0) or 0))

    conversion_rate = None
    if base["conversations"]:
        conversion_rate = round((sales_count / base["conversations"]) * 100, 2)

    return {
        "visits": base["visits"],
        "conversations": base["conversations"],
        "product_consultations": base["product_consultations"],
        "active_carts": base["active_carts"],
        "cart_actions": base["cart_actions"],
        "orders": order_count,
        "payment_attempts": payment_attempts,
        "approved_payments": approved_payments,
        "paid_orders": paid_orders,
        "sales": sales_count,
        "sales_amount": sales_amount,
        "conversion_rate": conversion_rate,
        "product_frequency": base["product_frequency"],
        "channels": {
            channel: {
                "visits": int(base["channels"].get(channel, {}).get("visits", 0)),
                "conversations": int(base["channels"].get(channel, {}).get("conversations", 0)),
                "orders": data["orders"],
                "paid_orders": data["paid_orders"],
                "sales": data["sales"],
                "amount": data["amount"],
            }
            for channel, data in channel_conversion.items()
        },
    }


def install_metrics_hooks(app) -> None:
    """Attach lightweight attribution hooks to the already-public Vendor routes."""
    if getattr(app, "_vendor_metrics_hooks_installed", False):
        return

    @app.after_request
    def _record_vendor_metrics(response):
        endpoint = str(request.endpoint or "")
        public_endpoints = {
            "vendor_publication.public_vendor_page",
            "vendor_publication.public_vendor_catalog",
            "vendor_publication.public_vendor_cart",
            "vendor_publication.public_vendor_checkout",
            "vendor_publication.public_vendor_message",
        }
        if endpoint not in public_endpoints or not 200 <= response.status_code < 300:
            return response

        try:
            from services.ai_agent.vendor_publication import _public_available_company, _public_conversation

            slug = str((request.view_args or {}).get("slug") or "").strip()
            company = _public_available_company(slug)
            if company is None:
                return response

            payload = request.get_json(silent=True) or {}
            conversation_id = payload.get("conversation_id") or request.args.get("conversation_id")
            create = endpoint != "vendor_publication.public_vendor_state"
            conversation = _public_conversation(company, conversation_id, create=create)
            if conversation is None:
                return response

            if endpoint == "vendor_publication.public_vendor_page":
                visitor_key = f"vendor_visit_recorded_{int(company.id)}_{slug}"
                if not session.get(visitor_key):
                    record_vendor_event(
                        conversation,
                        "visit",
                        unique_key=f"visit:{conversation.external_conversation_id}",
                    )
                    session[visitor_key] = True

            elif endpoint == "vendor_publication.public_vendor_catalog":
                data = response.get_json(silent=True) or {}
                products = data.get("products") if isinstance(data, dict) else []
                ids = [item.get("id") for item in products if isinstance(item, dict) and item.get("id")]
                record_vendor_event(conversation, "product_query", product_ids=ids)

            elif endpoint == "vendor_publication.public_vendor_cart":
                action = str(payload.get("action") or "add").strip().lower()
                if action in {"add", "remove"}:
                    record_vendor_event(
                        conversation,
                        f"cart_{action}",
                        product_ids=[payload.get("product_id")] if payload.get("product_id") else None,
                    )

            elif endpoint == "vendor_publication.public_vendor_checkout":
                data = response.get_json(silent=True) or {}
                if bool(data.get("success")):
                    record_vendor_event(conversation, "order_created")

            elif endpoint == "vendor_publication.public_vendor_message":
                record_vendor_event(
                    conversation,
                    "conversation_started",
                    unique_key=f"conversation_started:{conversation.id}",
                )

            db.session.commit()
        except Exception:
            db.session.rollback()
            app.logger.exception("No se pudo registrar métrica del Vendedor IA.")
        return response

    app._vendor_metrics_hooks_installed = True
