"""Tenant scoping helpers."""

import os

from sqlalchemy import false, inspect

from .constants import ROLE_SUPERADMIN
from .permissions import is_superadmin


def get_current_company_id(current_user_obj):
    if not getattr(current_user_obj, "is_authenticated", False):
        return None
    if is_superadmin(current_user_obj):
        return None
    return getattr(current_user_obj, "company_id", None)


def scope_query_to_company(query, model, *, current_user_obj, company_id=None):
    if not getattr(current_user_obj, "is_authenticated", False):
        return query
    if is_superadmin(current_user_obj):
        return query
    if not hasattr(model, "company_id"):
        return query
    effective_company_id = company_id if company_id is not None else get_current_company_id(current_user_obj)
    if effective_company_id is None:
        # Fail-closed: tenant users without company context must not read cross-tenant data.
        return query.filter(false())
    return query.filter(model.company_id == effective_company_id)


def model_table_exists(db_engine, model):
    table_name = getattr(model, "__tablename__", None)
    if not table_name:
        return False
    try:
        return inspect(db_engine).has_table(table_name)
    except Exception:
        return False


def is_control_panel_owner(user):
    owner_username = (os.environ.get("ADMIN_USERNAME") or "admin").strip().lower()
    owner_email = (os.environ.get("ADMIN_EMAIL") or "admin@stockarmobile.local").strip().lower()
    username = (getattr(user, "username", None) or "").strip().lower()
    email = (getattr(user, "email", None) or "").strip().lower()
    return username == owner_username or email == owner_email
