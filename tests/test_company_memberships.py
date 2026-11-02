from datetime import timedelta
import hashlib

from flask import session

from app import Company, CompanyMembership, User, db, load_user, utcnow
from stockarmobile.company_memberships import (
    ACTIVE_COMPANY_SESSION_KEY,
    active_memberships_for_user,
    apply_membership_context,
    get_company_users,
)


def _company(name):
    row = Company(name=name, active=True)
    db.session.add(row)
    db.session.flush()
    return row


def _user(company, *, role="admin", email="multi@example.test"):
    row = User(
        username="multi-user",
        email=email,
        company_id=company.id,
        role=role,
        active=True,
        permissions_json='["inventory"]',
    )
    row.set_password("secret123")
    db.session.add(row)
    db.session.flush()
    return row


def _membership(user, company, *, role, permissions="[]"):
    row = CompanyMembership.query.filter_by(user_id=user.id, company_id=company.id).first()
    if row is None:
        row = CompanyMembership(
            user_id=user.id,
            company_id=company.id,
            role=role,
            permissions_json=permissions,
            active=True,
            status="active",
            accepted_at=utcnow(),
        )
        db.session.add(row)
    else:
        row.role = role
        row.permissions_json = permissions
        row.active = True
        row.status = "active"
        row.accepted_at = utcnow()
    db.session.flush()
    return row


def test_membership_context_is_company_specific_and_does_not_rewrite_primary_company(app):
    with app.app_context():
        first = _company("Empresa principal")
        second = _company("Empresa asociada")
        user = _user(first)
        first_membership = _membership(user, first, role="admin", permissions='["inventory","cash"]')
        second_membership = _membership(user, second, role="user", permissions='["inventory"]')
        db.session.commit()

        apply_membership_context(user, second_membership)
        assert user.company_id == second.id
        assert user.role == "user"
        assert user.permissions_json == '["inventory"]'
        db.session.flush()
        db.session.expire(user)
        stored = db.session.get(User, user.id)
        assert stored.company_id == first.id
        assert stored.role == "admin"
        assert stored.permissions_json == '["inventory"]'

        users_first = get_company_users(first.id, active_only=True)
        users_second = get_company_users(second.id, active_only=True)
        assert [item.id for item in users_first] == [user.id]
        assert [item.id for item in users_second] == [user.id]
        assert active_memberships_for_user(user.id)


def test_invited_membership_is_not_an_active_company_until_accepted(app):
    with app.app_context():
        first = _company("Empresa A")
        second = _company("Empresa B")
        user = _user(first)
        _membership(user, first, role="admin")
        invited = CompanyMembership(
            user_id=user.id,
            company_id=second.id,
            role="user",
            permissions_json="[]",
            active=False,
            status="invited",
            invitation_token_hash=hashlib.sha256(b"single-use-token").hexdigest(),
            invitation_expires_at=utcnow() + timedelta(days=7),
        )
        db.session.add(invited)
        db.session.commit()

        assert [row.company_id for row in active_memberships_for_user(user.id)] == [first.id]
        assert [row.id for row in get_company_users(second.id, active_only=True)] == []


def test_revoked_membership_does_not_allow_tenant_switch(app):
    with app.app_context():
        first = _company("Empresa A")
        second = _company("Empresa B")
        user = _user(first)
        _membership(user, first, role="admin")
        revoked = CompanyMembership(
            user_id=user.id,
            company_id=second.id,
            role="admin",
            permissions_json="[]",
            active=False,
            status="revoked",
        )
        db.session.add(revoked)
        db.session.commit()

        assert all(row.company_id != second.id for row in active_memberships_for_user(user.id))
