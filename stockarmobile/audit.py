"""Audit logging helpers."""

from flask import request
from flask_login import current_user



def record_audit_entry(db_session, audit_log_model, get_current_company_id_func, *, action, entity=None, entity_id=None, detail=None, user_id=None, company_id=None, ip_address=None):
    db_session.add(
        audit_log_model(
            user_id=user_id if user_id is not None else (current_user.id if current_user.is_authenticated else None),
            company_id=company_id if company_id is not None else get_current_company_id_func(),
            action=action,
            entity=entity,
            entity_id=entity_id,
            detail=detail,
            ip_address=ip_address if ip_address is not None else (request.remote_addr if request else None),
        )
    )
