"""Company membership helpers for multi-company user accounts.

A User remains a single global identity. Membership-specific role, permissions and
company context are applied only to the authenticated/request-local ORM instance
using SQLAlchemy's committed-value API, so switching tenant never rewrites the
legacy users.company_id column.
"""
from __future__ import annotations

from sqlalchemy.orm.attributes import set_committed_value

ACTIVE_COMPANY_SESSION_KEY = "stockarmobile_active_company_id"


def get_membership(user_id: int, company_id: int, *, active_only: bool = False):
    from app import CompanyMembership

    query = CompanyMembership.query.filter_by(user_id=int(user_id), company_id=int(company_id))
    if active_only:
        query = query.filter_by(active=True, status="active")
    return query.first()


def apply_membership_context(user, membership):
    """Expose the selected company's membership attributes without persisting them."""
    if membership is None:
        return user
    set_committed_value(user, "company_id", int(membership.company_id))
    set_committed_value(user, "role", membership.role or "user")
    set_committed_value(user, "permissions_json", membership.permissions_json or "[]")
    # These are transient presentation attributes, not mapped database fields.
    user.company_membership_active = bool(membership.active and membership.status == "active")
    user.company_membership_role = membership.role or "user"
    user.company_membership_id = int(membership.id)
    user.company_membership_missing = False
    return user


def ensure_primary_membership(user, company_id: int, *, db_session=None):
    """Create a compatibility membership for a legacy user still tied to its primary tenant."""
    from app import CompanyMembership, db

    db_session = db_session or db.session
    company_id = int(company_id)
    membership = get_membership(user.id, company_id)
    if membership is not None:
        return membership
    # Never infer a secondary-company relationship from the legacy primary key.
    primary_company_id = db_session.query(type(user).company_id).filter(type(user).id == user.id).scalar()
    if primary_company_id is None or int(primary_company_id) != company_id:
        return None
    membership = CompanyMembership(
        user_id=user.id,
        company_id=company_id,
        role=user.role or "user",
        permissions_json=user.permissions_json or "[]",
        active=bool(user.active),
        status="active" if user.active else "revoked",
    )
    db_session.add(membership)
    db_session.flush()
    return membership


def get_company_users(company_id: int, *, active_only: bool | None = None, include_pending: bool = False):
    """Return unique identities linked to a company, applying that company's role context."""
    from app import CompanyMembership, User, db

    company_id = int(company_id)
    memberships = CompanyMembership.query.filter_by(company_id=company_id).all()
    membership_by_user = {int(row.user_id): row for row in memberships}
    users_by_id = {}
    if membership_by_user:
        for user in User.query.filter(User.id.in_(list(membership_by_user))).all():
            users_by_id[int(user.id)] = user

    # Backward compatibility for pre-migration/test-created users without a membership row.
    for user in User.query.filter_by(company_id=company_id).all():
        users_by_id.setdefault(int(user.id), user)

    result = []
    for user_id, user in users_by_id.items():
        if (getattr(user, "role", None) or "").strip().lower() == "superadmin":
            continue
        membership = membership_by_user.get(user_id)
        if membership is None:
            membership = ensure_primary_membership(user, company_id, db_session=db.session)
        if membership is not None:
            if membership.status == "invited" and not include_pending:
                continue
            apply_membership_context(user, membership)
            member_active = bool(membership.active and membership.status == "active" and user.active)
        else:
            # A legacy user can only be returned for their one stored primary company.
            member_active = bool(user.active)
            user.company_membership_active = member_active
            user.company_membership_role = user.role or "user"
        if active_only is True and not member_active:
            continue
        if active_only is False and member_active:
            continue
        result.append(user)

    result.sort(key=lambda item: ((item.first_name or "").casefold(), (item.last_name or "").casefold(), (item.username or "").casefold(), item.id))
    return result


def active_memberships_for_user(user_id: int):
    from app import Company, CompanyMembership

    return (
        CompanyMembership.query.join(Company, Company.id == CompanyMembership.company_id)
        .filter(
            CompanyMembership.user_id == int(user_id),
            CompanyMembership.active.is_(True),
            CompanyMembership.status == "active",
            Company.active.is_(True),
        )
        .order_by(Company.name.asc(), Company.id.asc())
        .all()
    )


def choose_membership_for_login(user, requested_company_id=None):
    """Validate the selected tenant against active memberships; fallback to primary."""
    from app import Company, CompanyMembership, db

    if (user.role or "").strip().lower() == "superadmin":
        return None

    memberships = active_memberships_for_user(user.id)
    if requested_company_id is not None:
        try:
            wanted = int(requested_company_id)
        except (TypeError, ValueError):
            wanted = None
        if wanted is not None:
            selected = next((row for row in memberships if int(row.company_id) == wanted), None)
            if selected is not None:
                return selected

    primary_id = db.session.query(type(user).company_id).filter(type(user).id == user.id).scalar()
    if primary_id is not None:
        selected = next((row for row in memberships if int(row.company_id) == int(primary_id)), None)
        if selected is not None:
            return selected
    # A valid non-primary membership always wins over a legacy primary-company
    # fallback. This supports gradual adoption when an older account is invited
    # to a second tenant before its primary membership has been backfilled.
    if memberships:
        return memberships[0]
    if primary_id is not None:
        company = db.session.get(Company, int(primary_id))
        if company is not None and company.active:
            return None
    return None
