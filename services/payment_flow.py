"""Central classification helpers for Payment rows."""

from __future__ import annotations

from datetime import datetime, timedelta
import json
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


def expire_pos_payment_draft(
    db_session,
    payment,
    *,
    now: datetime | None = None,
    max_age_hours: int = POS_DRAFT_TTL_HOURS,
) -> bool:
    """Expire one abandoned local POS draft and release its stock reservation."""
    if not is_stale_pos_draft(payment, now=now, max_age_hours=max_age_hours):
        return False
    release_pos_payment_reservation(db_session, payment)
    payment.status = "expired"
    from app import PaymentHistory
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
    return True


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
    ).with_for_update()
    if company_id is not None:
        query = query.filter(Payment.company_id == int(company_id))

    rows = query.order_by(Payment.id.asc()).all()
    expired = 0
    for payment in rows:
        if expire_pos_payment_draft(
            db_session,
            payment,
            now=current,
            max_age_hours=max_age_hours,
        ):
            expired += 1
    return expired




def release_pos_payment_reservation(db_session, payment) -> bool:
    """Release a POS draft's reserved branch stock exactly once."""
    if payment_flow(payment) != FLOW_POS:
        return False
    try:
        payload = json.loads(getattr(payment, "payload_json", None) or "{}")
    except (TypeError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    snapshot = payload.get("snapshot")
    if not isinstance(snapshot, dict) or snapshot.get("stock_reservation_released"):
        return False
    if not snapshot.get("stock_reserved"):
        snapshot["stock_reservation_released"] = True
        payment.payload_json = json.dumps(payload, ensure_ascii=False)
        return False
    branch_id = getattr(payment, "branch_id", None) or snapshot.get("branch_id")
    if not branch_id:
        # Legacy drafts have no trustworthy origin branch and were never reserved.
        snapshot["stock_reservation_released"] = True
        payment.payload_json = json.dumps(payload, ensure_ascii=False)
        return False

    from app import Product
    from stockarmobile.branch_inventory import release_branch_stock_reservation
    reservation_items = sorted(
        snapshot.get("items") or [],
        key=lambda item: int(item.get("productId") or item.get("product_id") or 0),
    )
    for item in reservation_items:
        product_id = int(item.get("productId") or item.get("product_id") or 0)
        quantity = item.get("quantity") or 0
        if product_id <= 0 or not quantity:
            continue
        product = Product.query.filter_by(
            id=product_id,
            company_id=int(payment.company_id),
        ).first()
        if product is None:
            raise ValueError("No se pudo localizar un producto reservado del cobro QR.")
        release_branch_stock_reservation(product, int(branch_id), quantity)

    snapshot["stock_reservation_released"] = True
    snapshot["stock_reservation_released_at"] = datetime.utcnow().isoformat()
    payment.payload_json = json.dumps(payload, ensure_ascii=False)
    return True


def pos_payment_has_live_stock_reservation(payment) -> bool:
    """Whether a POS payment still owns a verifiable reservation at its origin."""
    if payment_flow(payment) != FLOW_POS or not getattr(payment, "branch_id", None):
        return False
    try:
        payload = json.loads(getattr(payment, "payload_json", None) or "{}")
    except (TypeError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    snapshot = payload.get("snapshot")
    if not isinstance(snapshot, dict):
        return False
    if not (
        int(snapshot.get("branch_id") or 0) == int(payment.branch_id)
        and int(snapshot.get("cash_session_id") or 0) > 0
        and isinstance(snapshot.get("items"), list)
        and snapshot.get("items")
        and snapshot.get("stock_reserved")
        and not snapshot.get("stock_reservation_released")
    ):
        return False

    # Verify that the persisted branch ledger still holds every unit claimed
    # by the draft. The JSON flag alone is not sufficient after restores/manual
    # data repair or a partially migrated legacy payment.
    from decimal import Decimal, InvalidOperation
    from app import BranchStock
    for item in snapshot["items"]:
        try:
            product_id = int(item.get("productId") or item.get("product_id") or 0)
            quantity = Decimal(str(item.get("quantity") or 0))
            if product_id <= 0 or not quantity.is_finite() or quantity <= 0:
                return False
        except (TypeError, ValueError, InvalidOperation):
            return False
        stock_row = BranchStock.query.filter_by(
            company_id=int(payment.company_id),
            branch_id=int(payment.branch_id),
            product_id=product_id,
        ).first()
        if stock_row is None:
            return False
        if Decimal(str(stock_row.reserved_stock or 0)) < quantity:
            return False
    return True


def merge_pos_payment_provider_payload(payment, provider_payload: dict) -> None:
    """Persist Mercado Pago data without discarding the locally trusted sale snapshot."""
    try:
        payload = json.loads(getattr(payment, "payload_json", None) or "{}")
    except (TypeError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    payload.setdefault("flow", FLOW_POS)
    payload["mercadopago"] = provider_payload if isinstance(provider_payload, dict) else {}
    payment.payload_json = json.dumps(payload, ensure_ascii=False)


def _text(value: Any) -> str:
    return str(value or "").strip().lower()


def payment_flow(payment) -> str:
    provider = _text(getattr(payment, "provider", None))
    payment_method = _text(getattr(payment, "payment_method", None))
    external_reference = _text(getattr(payment, "external_reference", None))
    reference = _text(getattr(payment, "reference", None))
    subscription_id = getattr(payment, "subscription_id", None)

    if provider in {"mercadopago_ai_subscription", "mercadopago_ai_qr"} or payment_method in {"mercadopago_ai_subscription", "mercadopago_ai_qr"} or "ai_subscription:true" in external_reference or "ai_subscription_qr:true" in external_reference:
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
        func.coalesce(Payment.provider, "").in_(("mercadopago_ai_subscription", "mercadopago_ai_qr")),
        func.coalesce(Payment.payment_method, "").in_(("mercadopago_ai_subscription", "mercadopago_ai_qr")),
        func.coalesce(Payment.external_reference, "").contains("ai_subscription:true"),
        func.coalesce(Payment.external_reference, "").contains("ai_subscription_qr:true"),
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
