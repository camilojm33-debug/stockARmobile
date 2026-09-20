"""Notification center facade with the AI order stream layered on top."""

from __future__ import annotations

import json
from hashlib import sha256
from urllib.parse import urlparse

from flask_login import current_user

from stockarmobile.permissions import EMPLOYEE_ADMIN_ONLY, parse_permissions_json, user_role

from services.notification_service_legacy import (
    _build_recent_quote_acceptance_notifications,
    _build_seller_notifications,
    _build_superadmin_notifications,
    _build_user_notifications,
    _signature_for_items,
    _subscription_notification_target,
)


# Keep the legacy builders import-compatible for existing extensions and tests.
_NOTIFICATION_ROUTE_PERMISSIONS = {
    "/ventas/": "sales",
    "/productos/": "inventory",
    "/clientes/": "clients",
    "/reportes/": "reports",
    "/caja/": "cash",
    "/presupuestos/": "quotes_view",
    "/agentes-ia/": "ai_access",
    "/pedidos-ia": "ai_access",
    "/admin/portal": EMPLOYEE_ADMIN_ONLY,
    "/admin/company-settings": EMPLOYEE_ADMIN_ONLY,
    "/admin": EMPLOYEE_ADMIN_ONLY,
    "/compras/": EMPLOYEE_ADMIN_ONLY,
    "/gastos/": EMPLOYEE_ADMIN_ONLY,
    "/superadmin": EMPLOYEE_ADMIN_ONLY,
}


def _notification_required_permission(item):
    explicit = str(item.get("permission") or "").strip()
    if explicit:
        return explicit

    href = str(item.get("href") or "").strip()
    path = urlparse(href).path or href.split("?", 1)[0]
    path = "/" + path.lstrip("/")
    for prefix, permission in sorted(_NOTIFICATION_ROUTE_PERMISSIONS.items(), key=lambda row: -len(row[0])):
        if path.startswith(prefix):
            return permission
    return None


def _persisted_current_user():
    """Reload the signed-in user from the database before applying permissions.

    Flask-Login keeps a user object in the request context, while permission
    changes can be committed directly through another ORM operation. Using
    populate_existing() is intentional here: SQLAlchemy's identity map may
    otherwise return an already-loaded User without refreshing its columns.
    """
    login_user = current_user._get_current_object()
    from app import User

    return (
        User.query
        .populate_existing()
        .filter_by(id=login_user.id)
        .first()
    )


def filter_notifications_for_user(items):
    """Return only notifications allowed by the signed-in employee permission set.

    Admins, SuperAdmins and referral sellers keep their existing notification
    streams. Tenant employees (role=user) are fail-closed: a notification must
    map to an explicit permission or it is hidden rather than leaking a module
    or business information the employee cannot access.
    """
    persisted_user = _persisted_current_user()

    # If the persisted user cannot be resolved, fail closed for the employee
    # instead of falling back to a potentially stale Flask-Login permission set.
    if persisted_user is None:
        if user_role(current_user) == "user":
            return []
        return list(items or [])

    if user_role(persisted_user) != "user":
        return list(items or [])

    permissions = parse_permissions_json(getattr(persisted_user, "permissions_json", None))
    filtered = []
    for item in items or []:
        required = _notification_required_permission(item)
        if required == EMPLOYEE_ADMIN_ONLY:
            continue
        if required and required in permissions:
            filtered.append(item)
    return filtered


def build_notifications():
    if not getattr(current_user, "is_authenticated", False):
        return []
    if getattr(current_user, "role", None) == "superadmin":
        items = _build_superadmin_notifications()
        return filter_notifications_for_user(items)
    if getattr(current_user, "role", None) == "seller":
        items = _build_seller_notifications()
        return filter_notifications_for_user(items)

    # Lazy import is intentional: order_notifications imports the AI runtime,
    # which imports models that are initialized while the application is being
    # imported. Importing it at module import time creates a circular import.
    from services.ai_agent.order_notifications import build_ai_order_notifications

    items = build_ai_order_notifications() + _build_user_notifications()
    return filter_notifications_for_user(items)


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
