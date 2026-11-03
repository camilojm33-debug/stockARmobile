from datetime import timedelta
import hashlib
import re

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


def _assert_rendered_tenant_context(html, expected_company_id, expected_role, client):
    company_match = re.search(r'data-company-id="([^"]*)"', html)
    role_match = re.search(r'data-role="([^"]*)"', html)
    with client.session_transaction() as browser_session:
        selected_company_id = browser_session.get(ACTIVE_COMPANY_SESSION_KEY)
    actual_company_id = company_match.group(1) if company_match else None
    actual_role = role_match.group(1) if role_match else None
    assert actual_company_id == str(expected_company_id), (
        f"expected company_id={expected_company_id}, rendered={actual_company_id}, "
        f"session_company_id={selected_company_id}, role={actual_role}"
    )
    assert actual_role == expected_role, (
        f"expected role={expected_role}, rendered role={actual_role}, "
        f"company_id={actual_company_id}, session_company_id={selected_company_id}"
    )


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



def test_authenticated_user_can_switch_tenant_without_rewriting_primary_identity(company_membership_app):
    from flask import url_for

    with company_membership_app.app_context():
        first = _company("Empresa de origen")
        second = _company("Empresa asociada")
        third = _company("Empresa no autorizada")
        user = _user(first, role="admin", email="switch@example.test")
        _membership(user, first, role="admin", permissions='["inventory","cash"]')
        _membership(user, second, role="user", permissions='["inventory"]')
        user_id = user.id
        first_id = first.id
        second_id = second.id
        third_id = third.id
        db.session.commit()
        with company_membership_app.test_request_context():
            dashboard_url = url_for("dashboard.index")

    client = company_membership_app.test_client()
    login_response = client.post(
        "/auth/login",
        data={"username": "switch@example.test", "password": "secret123"},
        follow_redirects=False,
    )
    assert login_response.status_code in {302, 303}

    first_response = client.get(dashboard_url)
    assert first_response.status_code == 200
    assert f'data-company-id="{first_id}"' in first_response.get_data(as_text=True)
    assert 'data-role="admin"' in first_response.get_data(as_text=True)

    switched = client.post("/auth/switch-company", data={"company_id": str(second_id)})
    assert switched.status_code in {302, 303}
    with client.session_transaction() as active_session:
        assert active_session.get(ACTIVE_COMPANY_SESSION_KEY) == second_id
    second_response = client.get(dashboard_url)
    assert second_response.status_code == 200
    second_html = second_response.get_data(as_text=True)
    _assert_rendered_tenant_context(second_html, second_id, "user", client)

    denied = client.post("/auth/switch-company", data={"company_id": str(third_id)})
    assert denied.status_code == 403

    with company_membership_app.app_context():
        stored = db.session.get(User, user_id)
        assert stored.company_id == first_id
        assert stored.role == "admin"
        assert stored.permissions_json == '["inventory","cash"]'



def test_user_without_active_membership_can_only_accept_new_company_invitation(company_membership_app):
    from flask import url_for

    with company_membership_app.app_context():
        old_company = _company("Empresa anterior")
        new_company = _company("Nueva empresa")
        user = _user(old_company, role="admin", email="returning@example.test")
        old_membership = _membership(user, old_company, role="admin")
        old_membership.active = False
        old_membership.status = "revoked"
        token = "fresh-company-invitation"
        invited = CompanyMembership(
            user_id=user.id,
            company_id=new_company.id,
            role="user",
            permissions_json='["inventory"]',
            active=False,
            status="invited",
            invitation_token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
            invitation_expires_at=utcnow() + timedelta(days=7),
        )
        db.session.add(invited)
        db.session.commit()
        user_id = user.id
        old_company_id = old_company.id
        new_company_id = new_company.id
        with company_membership_app.test_request_context():
            dashboard_url = url_for("dashboard.index")

    client = company_membership_app.test_client()
    login_response = client.post(
        "/auth/login",
        data={"username": "returning@example.test", "password": "secret123"},
        follow_redirects=False,
    )
    assert login_response.status_code in {302, 303}

    invite_page = client.get(f"/auth/company-invitations/{token}/accept")
    assert invite_page.status_code == 200
    assert "Nueva empresa" in invite_page.get_data(as_text=True)

    accepted = client.post(f"/auth/company-invitations/{token}/accept")
    assert accepted.status_code in {302, 303}
    dashboard = client.get(dashboard_url)
    assert dashboard.status_code == 200
    html = dashboard.get_data(as_text=True)
    _assert_rendered_tenant_context(html, new_company_id, "user", client)

    with company_membership_app.app_context():
        stored = db.session.get(User, user_id)
        assert stored.company_id == old_company_id
        assert stored.active is True
        assert CompanyMembership.query.filter_by(
            user_id=user_id,
            company_id=new_company_id,
            status="active",
            active=True,
        ).first() is not None
