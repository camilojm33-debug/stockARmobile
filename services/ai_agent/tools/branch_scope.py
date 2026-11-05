"""Branch visibility helpers for AI tools."""
from __future__ import annotations


def employee_branch_scope(company_id: int) -> tuple[bool, int | None]:
    """Return (restricted, branch_id) for an authenticated tenant employee.

    Company administrators and unauthenticated channel agents retain company-wide
    scope. Employees are limited to their currently selected, assigned branch.
    A missing/mismatched employee context resolves to an impossible branch ID.
    """
    from flask_login import current_user

    if not getattr(current_user, "is_authenticated", False):
        return False, None

    role = str(getattr(current_user, "role", "") or "").strip().lower()
    if role in {"admin", "superadmin"}:
        return False, None

    try:
        expected_company_id = int(company_id)
        user_company_id = int(getattr(current_user, "company_id", 0) or 0)
    except (TypeError, ValueError):
        return True, -1
    if user_company_id != expected_company_id:
        return True, -1

    from stockarmobile.branch_context import get_active_branch_id
    branch_id = get_active_branch_id()
    return True, int(branch_id) if branch_id is not None else -1


def public_vendor_conversation_branch_scope(company_id: int, context: dict | None = None) -> tuple[bool, int | None]:
    """Resolve the server-persisted fulfillment branch for anonymous vendor chats.

    Never trust a branch ID from model arguments or channel metadata. The only
    accepted context key is the internal conversation ID created by the server.
    """
    payload = context if isinstance(context, dict) else {}
    channel = str(payload.get("channel") or "").strip().lower()
    if channel not in {"whatsapp", "whatsapp_business", "webchat"}:
        return False, None

    try:
        conversation_id = int(payload.get("conversation_id") or 0)
        tenant_id = int(company_id)
    except (TypeError, ValueError):
        return True, -1
    if conversation_id <= 0 or tenant_id <= 0:
        return True, -1

    from stockarmobile.models.conversations import Conversation
    conversation = Conversation.query.filter_by(
        id=conversation_id,
        company_id=tenant_id,
        channel=channel,
    ).first()
    if conversation is None:
        return True, -1

    from services.ai_agent.vendor_order_service import _vendor_conversation_branch_id
    try:
        branch_id = _vendor_conversation_branch_id(tenant_id, conversation)
    except ValueError:
        return True, -1
    return True, int(branch_id) if branch_id is not None else -1
