from flask import session, url_for
from flask_login import login_user

from app import Company, User, UserCompanyMembership, db
from stockarmobile.company_memberships import (
    ACTIVE_COMPANY_SESSION_KEY,
    available_company_choices,
    bind_active_company_context,
)


def _company(name):
    company = Company(name=name, active=True)
    db.session.add(company)
    db.session.flush()
    return company


def _user(company, username="shared-login"):
    user = User(
        username=username,
        email=f"{username}@example.test",
        company_id=company.id,
        role="admin",
        permissions_json='["sales"]',
        active=True,
    )
    user.set_password("test-password")
    db.session.add(user)
    db.session.flush()
    return user


def _membership(user, company, *, role, permissions, active=True):
    membership = UserCompanyMembership(
        user_id=user.id,
        company_id=company.id,
        role=role,
        permissions_json=permissions,
        active=active,
    )
    db.session.add(membership)
    db.session.flush()
    return membership


def test_one_login_can_switch_between_company_memberships_with_independent_roles(app):
    with app.app_context():
        company_a = _company("Grupo Norte")
        company_b = _company("Comercio Centro")
        user = _user(company_a)
        membership_a = _membership(
            user, company_a, role="admin", permissions='["sales","billing"]'
        )
        membership_b = _membership(
            user, company_b, role="user", permissions='["inventory"]'
        )
        user_id = user.id
        db.session.commit()

        with app.test_request_context("/"):
            login_user(user)
            session[ACTIVE_COMPANY_SESSION_KEY] = company_b.id

            selected = bind_active_company_context(user)

            assert selected.id == membership_b.id
            assert user.company_id == company_b.id
            assert user.role == "user"
            assert user.permissions_json == '["inventory"]'
            assert not db.session.is_modified(user)

            choices = available_company_choices(user)
            assert {item["id"] for item in choices} == {company_a.id, company_b.id}
            assert next(item for item in choices if item["id"] == company_b.id)["current"] is True

            db.session.flush()
            db.session.expire(db.session.get(User, user_id))
            persisted = db.session.get(User, user_id)
            assert persisted.company_id == company_a.id
            assert persisted.role == "admin"
            assert persisted.permissions_json == '["sales"]'

        assert UserCompanyMembership.query.filter_by(
            user_id=user_id
        ).count() == 2


def test_revoked_primary_company_is_not_reactivated_and_other_membership_is_selected(app):
    with app.app_context():
        company_a = _company("Empresa Principal")
        company_b = _company("Empresa Secundaria")
        user = _user(company_a, username="revoked-user")
        primary = _membership(
            user, company_a, role="admin", permissions='["billing"]', active=False
        )
        secondary = _membership(
            user, company_b, role="user", permissions='["inventory"]', active=True
        )
        db.session.commit()

        with app.test_request_context("/"):
            login_user(user)
            selected = bind_active_company_context(user)

            assert selected.id == secondary.id
            assert user.company_id == company_b.id
            assert user.role == "user"
            assert primary.active is False
            assert session[ACTIVE_COMPANY_SESSION_KEY] == company_b.id


def test_account_without_any_active_membership_fails_closed(app):
    with app.app_context():
        company = _company("Empresa Revocada")
        user = _user(company, username="no-access-user")
        membership = _membership(
            user, company, role="admin", permissions='["billing"]', active=False
        )
        db.session.commit()

        with app.test_request_context("/"):
            login_user(user)
            selected = bind_active_company_context(user)

            assert selected is None
            assert user.company_id is None
            assert user.role == "user"
            assert user.permissions_json == "[]"
            assert membership.active is False
            assert ACTIVE_COMPANY_SESSION_KEY not in session


def test_switch_company_route_only_accepts_an_active_membership(app):
    with app.app_context():
        company_a = _company("Empresa A")
        company_b = _company("Empresa B")
        company_c = _company("Empresa No Autorizada")
        user = _user(company_a, username="switch-route-user")
        _membership(user, company_a, role="admin", permissions='["billing"]')
        _membership(user, company_b, role="user", permissions='["inventory"]')
        db.session.commit()
        user_id = user.id
        client = app.test_client()

        with client.session_transaction() as session_data:
            session_data["_user_id"] = str(user_id)
            session_data["_fresh"] = True

        response = client.post(
            url_for("auth.switch_company"),
            data={"company_id": str(company_b.id)},
            follow_redirects=False,
        )
        assert response.status_code == 302
        with client.session_transaction() as session_data:
            assert session_data[ACTIVE_COMPANY_SESSION_KEY] == company_b.id

        rejected = client.post(
            url_for("auth.switch_company"),
            data={"company_id": str(company_c.id)},
            follow_redirects=False,
        )
        assert rejected.status_code == 403
        with client.session_transaction() as session_data:
            assert session_data[ACTIVE_COMPANY_SESSION_KEY] == company_b.id
