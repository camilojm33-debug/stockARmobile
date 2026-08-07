"""Role and permission helpers."""

import json

from .constants import ROLE_ADMIN, ROLE_SUPERADMIN


def user_role(user):
    return (getattr(user, "role", None) or "").strip().lower()


def is_superadmin(user):
    return user_role(user) == ROLE_SUPERADMIN


def is_admin(user):
    return user_role(user) == ROLE_ADMIN


def parse_permissions_json(raw_permissions):
    raw = (raw_permissions or "").strip()
    if not raw:
        return set()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return set()
    if not isinstance(payload, list):
        return set()
    return {str(item).strip().lower() for item in payload if str(item).strip()}


def has_any_permission(user, candidates):
    return bool(parse_permissions_json(getattr(user, "permissions_json", None)).intersection(set(candidates or [])))
