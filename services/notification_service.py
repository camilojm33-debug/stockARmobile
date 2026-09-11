"""Notification center facade with the AI order stream layered on top."""

from __future__ import annotations

import json
from hashlib import sha256

from flask_login import current_user

from services.notification_service_legacy import (
    _build_recent_quote_acceptance_notifications,
    _build_seller_notifications,
    _build_superadmin_notifications,
    _build_user_notifications,
    _signature_for_items,
    _subscription_notification_target,
)
from services.ai_agent.order_notifications import build_ai_order_notifications


# Keep the legacy builders import-compatible for existing extensions and tests.

def build_notifications():
    if not getattr(current_user, "is_authenticated", False):
        return []
    if getattr(current_user, "role", None) == "superadmin":
        return _build_superadmin_notifications()
    if getattr(current_user, "role", None) == "seller":
        return _build_seller_notifications()
    return build_ai_order_notifications() + _build_user_notifications()


def get_notification_payload():
    """Return the existing notification center plus AI seller orders."""
    items = build_notifications()
    if not getattr(current_user, "is_authenticated", False):
        return {"items": [], "count": 0, "signature": None}
    signature = _signature_for_items(items)
    if not items:
        return {"items": [], "count": 0, "signature": signature}
    try:
        from app import NotificationReadState
        state = NotificationReadState.query.filter_by(user_id=current_user.id).first()
        is_seen = bool(state and state.last_seen_signature == signature)
    except Exception:
        is_seen = False
    return {"items": items, "count": 0 if is_seen else len(items), "signature": signature}


def mark_notifications_seen():
    """Persist the complete notification signature, including AI orders."""
    if not getattr(current_user, "is_authenticated", False):
        return {"ok": False, "count": 0}
    from app import NotificationReadState, db, utcnow
    items = build_notifications()
    signature = _signature_for_items(items)
    state = NotificationReadState.query.filter_by(user_id=current_user.id).first()
    now = utcnow()
    if state is None:
        state = NotificationReadState(user_id=current_user.id, last_seen_signature=signature, last_seen_at=now)
        db.session.add(state)
    else:
        state.last_seen_signature = signature
        state.last_seen_at = now
        state.updated_at = now
    db.session.commit()
    return {"ok": True, "count": len(items), "signature": signature}
