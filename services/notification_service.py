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


def _notification_key(item):
    """Return a stable per-notification key from the rendered notification identity."""
    explicit = str(item.get("notification_key") or "").strip()
    if explicit:
        return explicit
    normalized = {
        "type": item.get("type"),
        "title": item.get("title"),
        "body": item.get("body"),
        "href": item.get("href"),
        "permission": item.get("permission"),
    }
    payload = json.dumps(normalized, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return sha256(payload.encode("utf-8")).hexdigest()


def _notification_items_with_keys(items):
    return [
        {**item, "notification_key": _notification_key(item)}
        for item in (items or [])
    ]


def _load_read_notification_keys(state):
    if state is None:
        return []
    raw = getattr(state, "read_notification_keys", None)
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(payload, list):
        return []
    return [str(value).strip() for value in payload if str(value).strip()]


def _save_read_notification_keys(state, keys, now):
    # Keep the persisted list bounded so a long-lived account cannot grow this
    # field without limit. Current notification keys are always kept.
    deduplicated = []
    seen = set()
    for key in keys:
        normalized = str(key).strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduplicated.append(normalized)
    state.read_notification_keys = json.dumps(deduplicated[-500:], ensure_ascii=False)
    state.last_seen_at = now
    state.updated_at = now


def _current_notification_state(items):
    """Return persisted read keys and the legacy all-seen compatibility flag."""
    from app import NotificationReadState

    signature = _signature_for_items(items)
    state = NotificationReadState.query.filter_by(user_id=current_user.id).first()
    read_keys = set(_load_read_notification_keys(state))
    legacy_all_seen = bool(
        state
        and not read_keys
        and state.last_seen_signature == signature
    )
    if legacy_all_seen:
        read_keys.update(_notification_key(item) for item in items)
    return state, signature, read_keys


def get_notification_payload():
    """Return unread notifications for the current user."""
    items = _notification_items_with_keys(build_notifications())
    if not getattr(current_user, "is_authenticated", False):
        return {"items": [], "count": 0, "signature": None}

    state, signature, read_keys = _current_notification_state(items)
    unread_items = [
        item for item in items
        if item["notification_key"] not in read_keys
    ]
    return {
        "items": unread_items,
        "count": len(unread_items),
        "signature": signature,
    }


def mark_notification_read(notification_key):
    """Persist one visible notification as read and return remaining unread count."""
    if not getattr(current_user, "is_authenticated", False):
        return {"ok": False, "count": 0}

    from app import NotificationReadState, db, utcnow

    requested_key = str(notification_key or "").strip()
    if not requested_key or len(requested_key) > 128:
        return {"ok": False, "count": 0}

    items = _notification_items_with_keys(build_notifications())
    visible_keys = {item["notification_key"] for item in items}
    if requested_key not in visible_keys:
        # Do not accept arbitrary keys: a user can only mark a notification
        # that belongs to their current, already permission-filtered stream.
        unread_count = len(items)
        try:
            state, signature, read_keys = _current_notification_state(items)
            unread_count = sum(1 for item in items if item["notification_key"] not in read_keys)
        except Exception:
            pass
        return {"ok": False, "count": unread_count}

    state, signature, read_keys = _current_notification_state(items)
    read_keys.add(requested_key)
    now = utcnow()
    if state is None:
        state = NotificationReadState(
            user_id=current_user.id,
            last_seen_signature=signature,
            last_seen_at=now,
        )
        db.session.add(state)
    state.last_seen_signature = signature
    _save_read_notification_keys(state, list(read_keys), now)
    db.session.commit()

    unread_count = sum(1 for item in items if item["notification_key"] not in read_keys)
    return {
        "ok": True,
        "count": unread_count,
        "signature": signature,
        "notification_key": requested_key,
    }


def mark_notifications_seen():
    """Compatibility endpoint: mark the current notification stream as read."""
    if not getattr(current_user, "is_authenticated", False):
        return {"ok": False, "count": 0}

    from app import NotificationReadState, db, utcnow

    items = _notification_items_with_keys(build_notifications())
    signature = _signature_for_items(items)
    state = NotificationReadState.query.filter_by(user_id=current_user.id).first()
    now = utcnow()
    read_keys = set(_load_read_notification_keys(state))
    read_keys.update(item["notification_key"] for item in items)
    if state is None:
        state = NotificationReadState(
            user_id=current_user.id,
            last_seen_signature=signature,
            last_seen_at=now,
        )
        db.session.add(state)
    state.last_seen_signature = signature
    _save_read_notification_keys(state, list(read_keys), now)
    db.session.commit()
    return {"ok": True, "count": 0, "signature": signature}
