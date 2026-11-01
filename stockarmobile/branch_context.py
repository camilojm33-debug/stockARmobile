"""Premium multi-branch tenant context and access checks."""
from flask import session
from flask_login import current_user

SESSION_KEY = "stockarmobile_active_branch_id"


def multibranch_enabled(company=None):
    if company is None:
        from app import Company
        company = Company.query.filter_by(id=getattr(current_user, "company_id", None)).first()
    if company is None:
        return False
    from services.ai_agent.usage_service import can_use_commercial_feature
    return bool(can_use_commercial_feature(company, "multi_branch").allowed)


def accessible_branches(company_id=None):
    from app import Branch, UserBranchAccess
    company_id = company_id or getattr(current_user, "company_id", None)
    if not company_id or not getattr(current_user, "is_authenticated", False):
        return []
    query = Branch.query.filter_by(company_id=int(company_id), active=True)
    if getattr(current_user, "role", None) not in {"admin", "superadmin"}:
        query = query.join(UserBranchAccess, UserBranchAccess.branch_id == Branch.id).filter(UserBranchAccess.user_id == int(current_user.id))
    return query.order_by(Branch.is_default.desc(), Branch.name.asc(), Branch.id.asc()).all()


def get_active_branch_id(*, require_feature=True):
    if not getattr(current_user, "is_authenticated", False):
        return None
    company_id = getattr(current_user, "company_id", None)
    if not company_id or (require_feature and not multibranch_enabled()):
        return None
    branches = accessible_branches(company_id)
    if not branches:
        return None
    valid_ids = {int(item.id) for item in branches}
    try:
        selected = int(session.get(SESSION_KEY)) if session.get(SESSION_KEY) is not None else None
    except (TypeError, ValueError):
        selected = None
    if selected not in valid_ids:
        default = next((item for item in branches if item.is_default), branches[0])
        selected = int(default.id)
        session[SESSION_KEY] = selected
    return selected


def get_active_branch():
    from app import Branch
    branch_id = get_active_branch_id()
    if branch_id is None:
        return None
    return Branch.query.filter_by(id=branch_id, company_id=getattr(current_user, "company_id", None), active=True).first()


def branch_id_for_record():
    return get_active_branch_id()
