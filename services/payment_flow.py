"""Central classification helpers for Payment rows."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, not_, or_


FLOW_STANDARD = "standard_subscription"
FLOW_AI_SUBSCRIPTION = "ai_subscription"
FLOW_POS = "pos_sale"
FLOW_AI_ORDER = "ai_order"
FLOW_OTHER = "other"

POS_DRAFT_TTL_HOURS = 72
POS_DRAFT_PENDING_STATUSES = ("pending", "in_process", "authorized")


def is_stale_pos_draft(payment, *, now: datetime | None = None, max_age_hours: int = POS_DRAFT_TTL_HOURS) -> bool:
    if payment_flow(payment) != FLOW_POS:
        return False
    if _text(getattr(payment, "status", None)) not in POS_DRAFT_PENDING_STATUSES:
        return False
    if getattr(payment, "payment_id", None):
        return False
    created_at = getattr(payment, "created_at", None)
    if created_at is None:
        return False
    current = now or datetime.utcnow()
    return created_at < current - timedelta(hours=max(1, int(max_age_hours)))


def expire_stale_pos_drafts(db_session, *, company_id: int | None = None, now: datetime | None = None, max_age_hours: int = POS_DRAFT_TTL_HOURS) -> int:
    """Expire abandoned local POS drafts without blocking a later Mercado Pago webhook.

    Webhooks identify POS drafts by draft_payment_id and may still transition an
    expired draft to the actual Mercado Pago status when a delayed payment arrives.
    """
    from app import Payment, PaymentHistory

    current = now or datetime.utcnow()
    cutoff = current - timedelta(hours=max(1, int(max_age_hours)))
    query = Payment.query.filter(
        pos_payment_filter(Payment),
        Payment.status.in_(POS_DRAFT_PENDING_STATUSES),
        Payment.payment_id.is_(None),
        Payment.created_at < cutoff,
    )
    if company_id is not None:
        query = query.filter(Payment.company_id == int(company_id))

    rows = query.order_by(Payment.id.asc()).all()
    for payment in rows:
        payment.status = "expired"
        db_session.add(
            PaymentHistory(
                payment_id=payment.id,
                company_id=payment.company_id,
                event="expired_stale_pos_draft",
                detail=f"POS draft expired after {max(1, int(max_age_hours))} hours without payment_id.",
                source="system",
                status="expired",
            )
        )
    return len(rows)


def _text(value: Any) -> str:
    return str(value or "").strip().lower()


def payment_flow(payment) -> str:
    provider = _text(getattr(payment, "provider", None))
    payment_method = _text(getattr(payment, "payment_method", None))
    external_reference = _text(getattr(payment, "external_reference", None))
    reference = _text(getattr(payment, "reference", None))
    subscription_id = getattr(payment, "subscription_id", None)

    if provider == "mercadopago_ai_subscription" or payment_method == "mercadopago_ai_subscription" or "ai_subscription:true" in external_reference:
        return FLOW_AI_SUBSCRIPTION
    if provider == "mercadopago_ai_order" or "flow:ai_order" in external_reference or "flow:ai_order" in reference:
        return FLOW_AI_ORDER
    if provider == "mercadopago_pos" or "flow:pos_sale" in external_reference or "flow:pos_sale" in reference:
        return FLOW_POS
    if subscription_id is not None or provider == "mercadopago_subscription" or payment_method == "mercadopago_subscription" or "flow:subscription_auto" in external_reference:
        return FLOW_STANDARD
    return FLOW_OTHER


def payment_flow_label(payment) -> str:
    return {
        FLOW_STANDARD: "Standard",
        FLOW_AI_SUBSCRIPTION: "Suscripción IA",
        FLOW_POS: "POS",
        FLOW_AI_ORDER: "Pedido IA",
        FLOW_OTHER: "Otro",
    }.get(payment_flow(payment), "Otro")


def is_ai_subscription_payment(payment) -> bool:
    return payment_flow(payment) == FLOW_AI_SUBSCRIPTION


def is_standard_subscription_payment(payment) -> bool:
    return payment_flow(payment) == FLOW_STANDARD


def ai_subscription_payment_filter(Payment):
    return or_(
        func.coalesce(Payment.provider, "") == "mercadopago_ai_subscription",
        func.coalesce(Payment.payment_method, "") == "mercadopago_ai_subscription",
        func.coalesce(Payment.external_reference, "").contains("ai_subscription:true"),
    )


def pos_payment_filter(Payment):
    return or_(
        func.coalesce(Payment.provider, "") == "mercadopago_pos",
        func.coalesce(Payment.external_reference, "").contains("flow:pos_sale"),
        func.coalesce(Payment.reference, "").contains("flow:pos_sale"),
    )


def ai_order_payment_filter(Payment):
    return or_(
        func.coalesce(Payment.provider, "") == "mercadopago_ai_order",
        func.coalesce(Payment.external_reference, "").contains("flow:ai_order"),
        func.coalesce(Payment.reference, "").contains("flow:ai_order"),
    )


def standard_subscription_payment_filter(Payment):
    return and_(
        not_(ai_subscription_payment_filter(Payment)),
        not_(pos_payment_filter(Payment)),
        not_(ai_order_payment_filter(Payment)),
        or_(
            Payment.subscription_id.isnot(None),
            func.coalesce(Payment.provider, "") == "mercadopago_subscription",
            func.coalesce(Payment.payment_method, "") == "mercadopago_subscription",
            func.coalesce(Payment.external_reference, "").contains("flow:subscription_auto"),
        ),
    )


def subscription_revenue_payment_filter(Payment):
    return or_(standard_subscription_payment_filter(Payment), ai_subscription_payment_filter(Payment))
