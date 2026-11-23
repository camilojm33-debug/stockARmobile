"""Notification center facade with the AI order stream layered on top."""

from __future__ import annotations

import json
from hashlib import sha256
from urllib.parse import urlparse

from flask_login import current_user
from sqlalchemy.exc import OperationalError, ProgrammingError

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

_NOTIFICATION_CATEGORY_LABELS = {
    "platform": "Plataforma",
    "billing": "Facturación",
    "referrals": "Referidos",
    "support": "Soporte",
    "system": "Sistema",
    "sales": "Ventas",
    "inventory": "Inventario",
    "clients": "Clientes",
    "reports": "Reportes",
    "cash": "Caja",
    "quotes": "Presupuestos",
    "ai": "Agentes IA",
    "purchases": "Compras",
    "expenses": "Gastos",
    "general": "General",
}

_NOTIFICATION_PERMISSION_CATEGORIES = {
    "sales": "sales",
    "inventory": "inventory",
    "clients": "clients",
    "reports": "reports",
    "cash": "cash",
    "quotes_view": "quotes",
    "ai_access": "ai",
}

_NOTIFICATION_TYPE_PRESENTATION = {
    "danger": ("critical", "Crítica", "bi-exclamation-octagon-fill", 0),
    "warning": ("high", "Prioritaria", "bi-exclamation-triangle-fill", 1),
    "primary": ("normal", "Operativa", "bi-bell-fill", 2),
    "success": ("normal", "Informativa", "bi-check-circle-fill", 3),
    "info": ("normal", "Informativa", "bi-info-circle-fill", 4),
    "secondary": ("normal", "Informativa", "bi-dot", 5),
}


def _notification_category(item, role=None):
    """Classify alerts into consistent areas without changing their access rules."""
    explicit = str(item.get("category") or "").strip().lower()
    if explicit in _NOTIFICATION_CATEGORY_LABELS:
        return explicit

    permission = str(item.get("permission") or "").strip().lower()
    if permission in _NOTIFICATION_PERMISSION_CATEGORIES:
        return _NOTIFICATION_PERMISSION_CATEGORIES[permission]

    title = str(item.get("title") or "").strip().lower()
    path = urlparse(str(item.get("href") or "")).path.lower()

    # SuperAdmin alerts often share a single destination, so their title is the
    # most reliable category signal and must be checked before the generic path.
    title_rules = (
        (("backup", "respaldo"), "system"),
        (("webhook", "evento del sistema", "sistema"), "system"),
        (("ayuda", "soporte", "ticket"), "support"),
        (("referid", "comision"), "referrals"),
        (("pedido ia", "vendedor ia", "agente ia", "envío", "envio"), "ai"),
        (("pago a proveedor", "compra", "proveedor"), "purchases"),
        (("alertas administrativas", "pago", "suscrip", "factur"), "billing"),
        (("empresa", "alta"), "platform"),
        (("presupuesto", "cotización", "cotizacion"), "quotes"),
        (("venta",), "sales"),
        (("stock", "inventario", "producto"), "inventory"),
        (("cliente",), "clients"),
        (("caja",), "cash"),
        (("compra", "proveedor"), "purchases"),
        (("gasto",), "expenses"),
        (("reporte",), "reports"),
    )
    for needles, category in title_rules:
        if any(needle in title for needle in needles):
            return category

    path_rules = (
        (("/superadmin",), "platform"),
        (("/soporte",), "support"),
        (("/referidos",), "referrals"),
        (("/admin/portal", "/subscription"), "billing"),
        (("/ventas",), "sales"),
        (("/productos",), "inventory"),
        (("/clientes",), "clients"),
        (("/reportes",), "reports"),
        (("/caja",), "cash"),
        (("/presupuestos",), "quotes"),
        (("/agentes-ia", "/pedidos-ia", "/ai-orders"), "ai"),
        (("/compras",), "purchases"),
        (("/gastos",), "expenses"),
    )
    for prefixes, category in path_rules:
        if any(path.startswith(prefix) for prefix in prefixes):
            return category

    return "platform" if role == "superadmin" else "general"


def prepare_notification_items(items, role=None):
    """Add consistent priority, category and action metadata to the notification feed.

    The original title/body/href/type fields remain unchanged for compatibility
    with older clients and integrations. Sorting is stable within each priority.
    """
    prepared = []
    for index, item in enumerate(items or []):
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "secondary").strip().lower()
        severity, severity_label, icon, priority = _NOTIFICATION_TYPE_PRESENTATION.get(
            item_type,
            ("normal", "Informativa", "bi-info-circle-fill", 5),
        )
        category = _notification_category(item, role=role)
        prepared.append(
            {
                **item,
                "category": category,
                "category_label": _NOTIFICATION_CATEGORY_LABELS[category],
                "severity": severity,
                "severity_label": severity_label,
                "icon": icon,
                "priority": priority,
                "is_urgent": severity in {"critical", "high"},
                "action_label": str(item.get("action_label") or "Revisar"),
            }
        )

    prepared.sort(key=lambda item: item["priority"])
    return prepared



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
    """Read persisted permission fields without refreshing Flask-Login's identity.

    A tenant membership temporarily overrides company_id, role and permissions
    on the request-local User object. Querying the User entity with
    populate_existing() would overwrite that selected-company context with the
    legacy primary-company columns. Read scalar columns into a detached value
    object instead, preserving the authenticated ORM identity.
    """
    from types import SimpleNamespace
    from app import User, db

    login_user = current_user._get_current_object()
    row = (
        db.session.query(User.role, User.permissions_json)
        .filter(User.id == login_user.id)
        .first()
    )
    if row is None:
        return None
    return SimpleNamespace(role=row.role, permissions_json=row.permissions_json)


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
    role = user_role(current_user)
    if role == "superadmin":
        items = _build_superadmin_notifications()
        return prepare_notification_items(filter_notifications_for_user(items), role=role)
    if role == "seller":
        items = _build_seller_notifications()
        return prepare_notification_items(filter_notifications_for_user(items), role=role)

    # Lazy import is intentional: order_notifications imports the AI runtime,
    # which imports models that are initialized while the application is being
    # imported. Importing it at module import time creates a circular import.
    from services.ai_agent.order_notifications import build_ai_order_notifications

    items = build_ai_order_notifications() + _build_user_notifications()
    return prepare_notification_items(filter_notifications_for_user(items), role=role)

def _build_notification_feed():
    """Normalize the final feed after notification extensions have run.

    Some modules wrap build_notifications() and append their own alerts after
    the facade returns. Reapplying employee filtering and presentation here
    keeps those extension-generated alerts inside the same permission boundary
    and ensures every alert has the metadata expected by the UI.
    """
    role = user_role(current_user)
    items = build_notifications()
    if role == "user":
        items = filter_notifications_for_user(items)
    return prepare_notification_items(items, role=role)



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
    try:
        state = NotificationReadState.query.filter_by(user_id=current_user.id).first()
    except (OperationalError, ProgrammingError):
        # Keep the notification center available during legacy/test databases
        # where the optional persistence table has not been created yet.
        return None, signature, set()
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
    if not getattr(current_user, "is_authenticated", False):
        return {"items": [], "count": 0, "urgent_count": 0, "signature": None}
    items = _notification_items_with_keys(_build_notification_feed())

    state, signature, read_keys = _current_notification_state(items)
    unread_items = [
        item for item in items
        if item["notification_key"] not in read_keys
    ]
    return {
        "items": unread_items,
        "count": len(unread_items),
        "urgent_count": sum(1 for item in unread_items if item.get("is_urgent")),
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

    items = _notification_items_with_keys(_build_notification_feed())
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

    items = _notification_items_with_keys(_build_notification_feed())
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
