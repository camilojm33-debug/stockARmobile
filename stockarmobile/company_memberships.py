"""Company membership and active-tenant context helpers.

A User row remains the stable login identity. Tenant-specific authorization lives
in UserCompanyMembership, while legacy routes keep using current_user.company_id,
role and permissions_json through a request-local committed-value overlay.
"""
from __future__ import annotations

from flask import session
from sqlalchemy.orm.attributes import set_committed_value

ACTIVE_COMPANY_SESSION_KEY = "stockarmobile_active_company_id"


def get_membership(user_id: int, company_id: int, *, active_only: bool = False):
    from app import UserCompanyMembership

    query = UserCompanyMembership.query.filter_by(
        user_id=int(user_id), company_id=int(company_id)
    )
    if active_only:
        query = query.filter(UserCompanyMembership.active.is_(True))
    return query.first()


def ensure_membership(
    db_session,
    user,
    company_id: int,
    *,
    role: str | None = None,
    permissions_json: str | None = None,
    active: bool = True,
):
    from app import UserCompanyMembership

    company_id = int(company_id)
    membership = get_membership(user.id, company_id)
    if membership is None:
        membership = UserCompanyMembership(
            user_id=user.id,
            company_id=company_id,
            role=(role or getattr(user, "role", None) or "user"),
            permissions_json=(
                permissions_json
                if permissions_json is not None
                else (getattr(user, "permissions_json", None) or "[]")
            ),
            active=bool(active),
        )
        db_session.add(membership)
        db_session.flush()
    else:
        if role is not None:
            membership.role = role
        if permissions_json is not None:
            membership.permissions_json = permissions_json
        membership.active = bool(active)
    return membership


def membership_for_current_company(user=None):
    from flask_login import current_user

    user = user or current_user
    if not getattr(user, "is_authenticated", False):
        return None
    company_id = getattr(user, "company_id", None)
    if not company_id:
        return None
    return get_membership(user.id, company_id, active_only=True)


def bind_active_company_context(user):
    """Bind the selected active membership without persisting it to users.company_id."""
    if not getattr(user, "is_authenticated", False):
        return None
    if (getattr(user, "role", None) or "").strip().lower() == "superadmin":
        session.pop(ACTIVE_COMPANY_SESSION_KEY, None)
        return None

    from app import Company, UserCompanyMembership, db

    base_company_id = getattr(user, "company_id", None)
    if not base_company_id:
        return None

    try:
        selected_company_id = int(session.get(ACTIVE_COMPANY_SESSION_KEY) or base_company_id)
    except (TypeError, ValueError):
        selected_company_id = int(base_company_id)

    membership = get_membership(user.id, selected_company_id, active_only=True)
    if membership is None and selected_company_id == int(base_company_id):
        # Compatibility bridge for old/test-created users not yet backfilled.
        membership = ensure_membership(
            db.session,
            user,
            int(base_company_id),
            role=getattr(user, "role", None),
            permissions_json=getattr(user, "permissions_json", None),
            active=bool(getattr(user, "active", True)),
        )
    if membership is None:
        # Prefer the original tenant when active; otherwise let the account
        # continue through another explicitly granted company membership.
        selected_company_id = int(base_company_id)
        membership = get_membership(user.id, selected_company_id, active_only=True)
    if membership is None and bool(getattr(user, "active", True)):
        membership = (
            UserCompanyMembership.query
            .join(Company, Company.id == UserCompanyMembership.company_id)
            .filter(
                UserCompanyMembership.user_id == int(user.id),
                UserCompanyMembership.active.is_(True),
                Company.active.is_(True),
            )
            .order_by(UserCompanyMembership.company_id.asc())
            .first()
        )
        if membership is not None:
            selected_company_id = int(membership.company_id)

    if membership is None or not bool(getattr(user, "active", True)):
        session.pop(ACTIVE_COMPANY_SESSION_KEY, None)
        return None

    company = db.session.get(Company, selected_company_id)
    if company is None:
        session.pop(ACTIVE_COMPANY_SESSION_KEY, None)
        return None

    session[ACTIVE_COMPANY_SESSION_KEY] = selected_company_id
    set_committed_value(user, "company_id", selected_company_id)
    set_committed_value(user, "role", membership.role or "user")
    set_committed_value(user, "permissions_json", membership.permissions_json or "[]")
    return membership


def available_company_choices(user):
    """Return active company memberships for the authenticated account."""
    if not getattr(user, "is_authenticated", False):
        return []
    if (getattr(user, "role", None) or "").strip().lower() == "superadmin":
        return []

    from app import Company, UserCompanyMembership, db

    rows = (
        db.session.query(UserCompanyMembership, Company)
        .join(Company, Company.id == UserCompanyMembership.company_id)
        .filter(
            UserCompanyMembership.user_id == int(user.id),
            UserCompanyMembership.active.is_(True),
            Company.active.is_(True),
        )
        .order_by(Company.name.asc(), Company.id.asc())
        .all()
    )
    choices = []
    seen = set()
    for membership, company in rows:
        if company.id in seen:
            continue
        seen.add(company.id)
        choices.append({
            "id": int(company.id),
            "name": company.name,
            "role": membership.role or "user",
            "current": int(company.id) == int(getattr(user, "company_id", 0) or 0),
        })
    return choices


def apply_membership_to_user(user, membership):
    """Apply per-company fields to a User instance for read-only tenant UI rows."""
    set_committed_value(user, "company_id", int(membership.company_id))
    set_committed_value(user, "role", membership.role or "user")
    set_committed_value(user, "permissions_json", membership.permissions_json or "[]")
    set_committed_value(
        user, "active",
        bool(getattr(user, "active", True) and getattr(membership, "active", False)),
    )
    return user
