"""Value-oriented metrics for the tenant business dashboard.

This layer is intentionally read-only: it projects existing sales, clients,
products, quotes and AI order/payment data into a compact business-value
summary. It does not create a second source of truth.
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, or_

from services.sales_calculation_service import to_decimal
from stockarmobile.helpers.dates import local_month_start_utc_naive


CONFIRMED_STATUSES = {"confirmada", "confirmed", "aprobada", "approved", "completada", "complete"}
PENDING_QUOTE_STATUSES = {"BORRADOR", "ENVIADO"}


def _confirmed(model):
    return or_(model.status.is_(None), func.lower(model.status).in_(list(CONFIRMED_STATUSES)))


def _metadata(value):
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}") if isinstance(value, str) else {}
    except (TypeError, ValueError):
        parsed = {}
    return parsed if isinstance(parsed, dict) else {}


def _ai_flags(sale):
    metadata = _metadata(getattr(sale, "metadata_json", None))
    origin = str(metadata.get("origin") or metadata.get("ai_origin") or "").strip().lower()
    return metadata, origin in {"ai_vendor", "vendedor_ia", "vendedor"}


def _conversation_was_recovered(conversation_id: int | None, *, company_id: int) -> bool:
    if not conversation_id:
        return False
    from stockarmobile.models.conversations import Conversation
    conversation = Conversation.query.filter_by(id=int(conversation_id), company_id=int(company_id)).first()
    if conversation is None:
        return False
    metadata = _metadata(conversation.metadata_json)
    outbox = metadata.get("ai_outbox")
    return any(isinstance(item, dict) and item.get("status") == "sent" for item in (outbox if isinstance(outbox, list) else []))


def build_business_value_metrics(*, company, can_view_economic_metrics: bool) -> dict:
    """Return dashboard KPIs scoped to the active company."""
    from app import Client, Payment, Product, Quote, Sale, db, scope_query_to_company

    company_id = getattr(company, "id", None)
    if company_id is None:
        return _empty()

    timezone_name = getattr(company, "timezone", None) or "America/Argentina/Buenos_Aires"
    month_start = local_month_start_utc_naive(timezone_name)
    recoverable_cutoff = month_start - timedelta(days=60)

    sales_base = scope_query_to_company(Sale.query.filter(_confirmed(Sale)), Sale)
    month_sales = sales_base.filter(Sale.date >= month_start).all()
    sales_month = sum((to_decimal(getattr(row, "total_amount", 0)) for row in month_sales), Decimal("0.00"))

    previous_start = month_start - timedelta(days=31)
    previous_sales = sales_base.filter(Sale.date >= previous_start, Sale.date < month_start).all()
    sales_previous = sum((to_decimal(getattr(row, "total_amount", 0)) for row in previous_sales), Decimal("0.00"))
    sales_change_pct = ((sales_month - sales_previous) / sales_previous * Decimal("100")) if sales_previous else None

    critical_products = scope_query_to_company(
        Product.query.filter(Product.active.is_(True), Product.stock <= Product.min_stock), Product
    ).count()

    last_purchase = (
        db.session.query(Sale.client_id.label("client_id"), func.max(Sale.date).label("last_purchase"))
        .filter(Sale.company_id == company_id, Sale.client_id.isnot(None), _confirmed(Sale))
        .group_by(Sale.client_id)
        .subquery()
    )
    recoverable_clients = (
        db.session.query(func.count(Client.id))
        .join(last_purchase, last_purchase.c.client_id == Client.id)
        .filter(Client.company_id == company_id, Client.active.is_(True), last_purchase.c.last_purchase < recoverable_cutoff)
        .scalar()
        or 0
    )

    pending_orders = 0
    pending_orders_amount = Decimal("0.00")
    if getattr(Quote, "__tablename__", None):
        pending_query = scope_query_to_company(Quote.query.filter(Quote.status.in_(PENDING_QUOTE_STATUSES)), Quote)
        pending_orders = pending_query.count()
        if can_view_economic_metrics:
            pending_orders_amount = to_decimal(pending_query.with_entities(func.coalesce(func.sum(Quote.total_amount), 0)).scalar())

    ai_sales_count = 0
    ai_sales_amount = Decimal("0.00")
    recovered_amount = Decimal("0.00")
    recovered_sales_count = 0

    # The existing AI vendor flow stores the quote and conversation IDs in
    # Payment.external_reference. Avoid relying on a Payment.quote_id field.
    ai_payments = Payment.query.filter_by(
        company_id=company_id,
        provider="mercadopago_ai_order",
    ).all()
    payment_by_quote = {}
    for payment in ai_payments:
        match = re.search(
            r"(?:^|\|)quote_id:(\d+)(?:\||$)",
            str(getattr(payment, "external_reference", "") or ""),
        )
        if match:
            payment_by_quote[int(match.group(1))] = str(payment.external_reference or "")

    ai_rows = []
    if payment_by_quote:
        ai_rows = (
            db.session.query(Sale, Quote.id)
            .join(Quote, Quote.converted_sale_id == Sale.id)
            .filter(
                Sale.company_id == company_id,
                Sale.date >= month_start,
                _confirmed(Sale),
                Quote.id.in_(list(payment_by_quote.keys())),
            )
            .all()
        )

    seen_sale_ids = set()
    for sale, quote_id in ai_rows:
        if sale.id in seen_sale_ids:
            continue
        seen_sale_ids.add(sale.id)
        ai_sales_count += 1
        amount = to_decimal(getattr(sale, "total_amount", 0))
        ai_sales_amount += amount
        external_reference = payment_by_quote.get(int(quote_id), "")
        match = re.search(r"(?:^|\|)conversation_id:(\d+)(?:\||$)", external_reference)
        conversation_id = int(match.group(1)) if match else None
        if _conversation_was_recovered(conversation_id, company_id=company_id):
            recovered_sales_count += 1
            recovered_amount += amount

    # Also accept explicit attribution if a future flow writes it directly on Sale.
    for sale in month_sales:
        metadata, is_ai = _ai_flags(sale)
        if not is_ai or sale.id in seen_sale_ids:
            continue
        seen_sale_ids.add(sale.id)
        amount = to_decimal(getattr(sale, "total_amount", 0))
        ai_sales_count += 1
        ai_sales_amount += amount
        if metadata.get("ai_recovered") or metadata.get("recovered_by_ai"):
            recovered_sales_count += 1
            recovered_amount += amount

    opportunities = []
    if recoverable_clients:
        opportunities.append({
            "icon": "bi-people",
            "title": f"{recoverable_clients} cliente(s) recuperable(s)",
            "text": "Clientes con historial de compra que llevan 60 días o más sin volver.",
            "url": "/clientes/",
            "action": "Ver clientes",
        })
    if critical_products:
        opportunities.append({
            "icon": "bi-box-seam",
            "title": f"{critical_products} producto(s) con stock crítico",
            "text": "Hay inventario que puede afectar próximas ventas.",
            "url": "/productos/",
            "action": "Ver stock",
        })
    if pending_orders:
        opportunities.append({
            "icon": "bi-file-earmark-text",
            "title": f"{pending_orders} pedido(s)/presupuesto(s) pendientes",
            "text": "Operaciones que todavía pueden convertirse en venta.",
            "url": "/presupuestos/",
            "action": "Ver pendientes",
        })

    return {
        "sales_month": sales_month if can_view_economic_metrics else None,
        "sales_previous": sales_previous if can_view_economic_metrics else None,
        "sales_change_pct": sales_change_pct if can_view_economic_metrics else None,
        "critical_stock": critical_products,
        "recoverable_clients": recoverable_clients,
        "pending_orders": pending_orders,
        "pending_orders_amount": pending_orders_amount if can_view_economic_metrics else None,
        "ai_sales_count": ai_sales_count if can_view_economic_metrics else None,
        "ai_sales_amount": ai_sales_amount if can_view_economic_metrics else None,
        "recovered_sales_count": recovered_sales_count if can_view_economic_metrics else None,
        "recovered_amount": recovered_amount if can_view_economic_metrics else None,
        "opportunities": opportunities,
    }


def _empty():
    return {
        "sales_month": None,
        "sales_previous": None,
        "sales_change_pct": None,
        "critical_stock": 0,
        "recoverable_clients": 0,
        "pending_orders": 0,
        "pending_orders_amount": None,
        "ai_sales_count": None,
        "ai_sales_amount": None,
        "recovered_sales_count": None,
        "recovered_amount": None,
        "opportunities": [],
    }
