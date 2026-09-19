"""Role and permission helpers."""

import json

from .constants import ROLE_ADMIN, ROLE_SUPERADMIN

AI_ACCESS_PERMISSION = "ai_access"


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


EMPLOYEE_ADMIN_ONLY = "__admin_only__"


def employee_endpoint_permission(endpoint: str | None, method: str = "GET"):
    """Return the explicit permission required by an employee-only route.

    Admins/superadmins bypass this map. Employees (role=user) must have the
    returned permission; routes without a mapping remain available to the
    authenticated employee base experience (dashboard/help/auth).
    """
    ep = str(endpoint or "").strip().lower()
    http_method = str(method or "GET").upper()

    if ep.startswith(("sales.",)):
        return "sales"
    if ep.startswith(("products.",)):
        return "inventory"
    if ep.startswith(("clients.",)):
        return "clients"
    if ep.startswith(("reports.",)):
        return "reports"
    if ep.startswith(("cash.",)):
        return "cash"
    if ep.startswith(("qr_labels.",)):
        return "inventory"
    if ep.startswith(("expenses.",)):
        return EMPLOYEE_ADMIN_ONLY
    if ep.startswith(("purchases.",)):
        return EMPLOYEE_ADMIN_ONLY
    if ep.startswith(("ai_agents.",)):
        return "ai_access"

    if ep in {"whatsapp_agent.ai_orders", "whatsapp_agent.ai_order_detail"}:
        return "ai_access"

    if ep == "company_billing.business_billing_hub":
        return "billing"
    if ep.startswith("company_billing.company_settings"):
        return EMPLOYEE_ADMIN_ONLY
    if ep == "company_billing.subscription_portal":
        return EMPLOYEE_ADMIN_ONLY

    if ep.startswith("quotes."):
        quote_permissions = {
            "quotes.index": "quotes_view",
            "quotes.new_quote": "quotes_create",
            "quotes.view_quote": "quotes_view",
            "quotes.edit_quote": "quotes_edit",
            "quotes.duplicate_quote": "quotes_duplicate",
            "quotes.delete_quote": "quotes_delete",
            "quotes.annul_quote": "quotes_anulate",
            "quotes.quote_pdf": "quotes_download_pdf",
            "quotes.quote_print": "quotes_print",
            "quotes.share_whatsapp": "quotes_share_whatsapp",
            "quotes.share_whatsapp_post": "quotes_share_whatsapp",
            "quotes.convert_to_sale": "quotes_convert",
            "quotes.email_quote": "quotes_email",
        }
        required = quote_permissions.get(ep)
        if required:
            return required
        # Unknown authenticated quote routes fail closed for employees.
        return "quotes_view"

    return None


def can_access_ai(user):
    """Tenant AI access: admins are allowed; employees need the explicit ai_access permission."""
    role = user_role(user)
    if role in {ROLE_ADMIN, ROLE_SUPERADMIN}:
        return True
    return AI_ACCESS_PERMISSION in parse_permissions_json(getattr(user, "permissions_json", None))
