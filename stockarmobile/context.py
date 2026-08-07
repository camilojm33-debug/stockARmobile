"""Request/user context helpers."""

from flask import g

from .permissions import user_role
from .tenant import get_current_company_id


def bind_current_tenant_context(current_user_obj):
    if getattr(current_user_obj, "is_authenticated", False):
        g.current_company_id = get_current_company_id(current_user_obj)
        g.current_user_role = user_role(current_user_obj)
    else:
        g.current_company_id = None
        g.current_user_role = None
