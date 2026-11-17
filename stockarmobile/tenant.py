"""Tenant scoping helpers."""

from sqlalchemy import false, inspect

from .permissions import is_superadmin


def get_current_company_id(current_user_obj):
    if not getattr(current_user_obj, "is_authenticated", False):
        return None
    if is_superadmin(current_user_obj):
        return None
    return getattr(current_user_obj, "company_id", None)


def scope_query_to_company(query, model, *, current_user_obj, company_id=None):
    """Constrain tenant-owned queries to the active tenant, failing closed.

    The optional company_id is retained for compatibility, but it may not
    widen an authenticated tenant user's scope. SuperAdmin keeps its
    explicitly privileged, unscoped behavior. Models without company_id are
    shared/system models and cannot be tenant-filtered here.
    """
    if not hasattr(model, "company_id"):
        return query
    if not getattr(current_user_obj, "is_authenticated", False):
        return query.filter(false())
    if is_superadmin(current_user_obj):
        return query

    effective_company_id = get_current_company_id(current_user_obj)
    if effective_company_id is None:
        # Fail-closed: tenant users without company context must not read
        # cross-tenant data.
        return query.filter(false())

    if company_id is not None:
        try:
            requested_company_id = int(company_id)
            active_company_id = int(effective_company_id)
        except (TypeError, ValueError, OverflowError):
            return query.filter(false())
        if requested_company_id != active_company_id:
            # A caller-supplied tenant ID must never override the authenticated
            # user's active membership.
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
