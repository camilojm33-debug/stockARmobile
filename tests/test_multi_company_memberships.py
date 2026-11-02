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

        with app.app_context():
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

        with app.test_request_context("/"):
            switch_url = url_for("auth.switch_company")
        response = client.post(
            switch_url,
            data={"company_id": str(company_b.id)},
            follow_redirects=False,
        )
        assert response.status_code == 302
        with client.session_transaction() as session_data:
            assert session_data[ACTIVE_COMPANY_SESSION_KEY] == company_b.id

        rejected = client.post(
            switch_url,
            data={"company_id": str(company_c.id)},
            follow_redirects=False,
        )
        assert rejected.status_code == 403
        with client.session_transaction() as session_data:
            assert session_data[ACTIVE_COMPANY_SESSION_KEY] == company_b.id



def test_company_invitation_remains_pending_until_user_accepts(app, monkeypatch):
    from app import Branch, UserBranchAccess
    from services.company_membership_invitation_service import CompanyMembershipInvitationService
    from services.plan_service import PlanService
    from services.subscription_service import SubscriptionService

    monkeypatch.setattr(
        CompanyMembershipInvitationService,
        "send_invitation_email",
        staticmethod(lambda **kwargs: False),
    )

    with app.app_context():
        company_a = _company("Compañía de origen")
        company_b = _company("Compañía invitante")
        PlanService.ensure_defaults(db.session)
        trial_plan = PlanService.get_plan(code="trial")
        SubscriptionService.ensure_company_trial(
            db.session, company=company_b, trial_plan=trial_plan
        )
        user = _user(company_a, username="invited-existing-user")
        _membership(user, company_a, role="admin", permissions='["billing"]')
        default_branch = Branch(
            company_id=company_b.id,
            name="Casa central",
            code="CASA-CENTRAL",
            active=True,
            is_default=True,
        )
        db.session.add(default_branch)
        db.session.flush()

        with app.test_request_context("/"):
            membership, invitation_url, email_sent = CompanyMembershipInvitationService.create_or_refresh(
                db.session,
                user=user,
                company=company_b,
                role="user",
                permissions_json='["inventory"]',
            )
            token = app.config["_LAST_COMPANY_INVITATION_TOKEN"]
            assert invitation_url.endswith(token)
            assert email_sent is False

        db.session.commit()
        assert membership.active is False
        assert membership.invitation_acceptance_required is True
        assert membership.invitation_token_hash
        assert membership.invitation_expires_at is not None

        with app.test_request_context("/"):
            login_user(user)
            choices = available_company_choices(user)
            assert company_b.id not in {item["id"] for item in choices}

        client = app.test_client()
        with client.session_transaction() as session_data:
            session_data["_user_id"] = str(user.id)
            session_data["_fresh"] = True

        with app.test_request_context("/"):
            invitation_path = url_for("auth.accept_company_invitation", token=token)
        page = client.get(invitation_path)
        assert page.status_code == 200
        assert b"Aceptar invitaci" in page.data

        accepted = client.post(invitation_path, data={}, follow_redirects=False)
        assert accepted.status_code == 302
        refreshed_membership = UserCompanyMembership.query.filter_by(
            user_id=user.id, company_id=company_b.id
        ).first()
        assert refreshed_membership.active is True
        assert refreshed_membership.accepted_at is not None
        assert refreshed_membership.invitation_token_hash is None
        assert refreshed_membership.invitation_acceptance_required is False
        assert UserBranchAccess.query.filter_by(
            user_id=user.id, company_id=company_b.id, branch_id=default_branch.id
        ).first() is not None

        with client.session_transaction() as session_data:
            assert session_data[ACTIVE_COMPANY_SESSION_KEY] == company_b.id

        replay = client.get(invitation_path)
        assert replay.status_code == 302


def test_invitation_cannot_be_accepted_by_a_different_user(app):
    from services.company_membership_invitation_service import CompanyMembershipInvitationService

    with app.app_context():
        company_a = _company("Empresa titular")
        company_b = _company("Empresa destino")
        owner = _user(company_a, username="invitation-owner")
        other = _user(company_a, username="different-login")
        _membership(owner, company_a, role="admin", permissions='["billing"]')
        _membership(other, company_a, role="user", permissions='["inventory"]')
        db.session.flush()

        with app.test_request_context("/"):
            _membership_row, _url, _sent = CompanyMembershipInvitationService.create_or_refresh(
                db.session,
                user=owner,
                company=company_b,
                role="user",
            )
            token = app.config["_LAST_COMPANY_INVITATION_TOKEN"]
        db.session.commit()

        client = app.test_client()
        with client.session_transaction() as session_data:
            session_data["_user_id"] = str(other.id)
            session_data["_fresh"] = True

        with app.test_request_context("/"):
            path = url_for("auth.accept_company_invitation", token=token)
        response = client.get(path)
        assert response.status_code == 403
        pending = UserCompanyMembership.query.filter_by(
            user_id=owner.id, company_id=company_b.id
        ).first()
        assert pending.active is False
        assert pending.invitation_token_hash is not None



def test_new_invited_account_sets_password_from_link_without_existing_login(app, monkeypatch):
    from app import Branch, User
    from services.company_membership_invitation_service import CompanyMembershipInvitationService
    from services.plan_service import PlanService
    from services.subscription_service import SubscriptionService

    monkeypatch.setattr(
        CompanyMembershipInvitationService,
        "send_invitation_email",
        staticmethod(lambda **kwargs: False),
    )
    with app.app_context():
        company = _company("Empresa con alta nueva")
        PlanService.ensure_defaults(db.session)
        trial_plan = PlanService.get_plan(code="trial")
        SubscriptionService.ensure_company_trial(
            db.session, company=company, trial_plan=trial_plan
        )
        branch = Branch(
            company_id=company.id,
            name="Casa central",
            code="CASA-CENTRAL",
            active=True,
            is_default=True,
        )
        user = User(
            username="new-invitee",
            email="new-invitee@example.test",
            company_id=company.id,
            role="user",
            active=True,
            must_change_password=True,
            auth_provider="local",
        )
        user.set_password("unusable-random-initial-password")
        db.session.add_all([branch, user])
        db.session.flush()
        with app.test_request_context("/"):
            membership, _url, _sent = CompanyMembershipInvitationService.create_or_refresh(
                db.session,
                user=user,
                company=company,
                role="user",
                password_setup_required=True,
            )
            token = app.config["_LAST_COMPANY_INVITATION_TOKEN"]
        db.session.commit()
        user_id = user.id
        company_id = company.id
        assert membership.active is False

        client = app.test_client()
        path = f"/auth/accept-company-invitation/{token}"
        page = client.get(path)
        assert page.status_code == 200
        assert b"Crear contrase" in page.data

        accepted = client.post(
            path,
            data={
                "new_password": "a-secure-new-password",
                "confirm_password": "a-secure-new-password",
            },
            follow_redirects=False,
        )
        assert accepted.status_code == 302
        refreshed = db.session.get(User, user_id)
        db.session.refresh(refreshed)
        refreshed_membership = UserCompanyMembership.query.filter_by(
            user_id=user_id, company_id=company_id
        ).first()
        assert refreshed.check_password("a-secure-new-password")
        assert refreshed.must_change_password is False
        assert refreshed_membership.active is True
        assert refreshed_membership.invitation_password_setup_required is False
        assert refreshed_membership.invitation_acceptance_required is False

        with client.session_transaction() as session_data:
            assert session_data[ACTIVE_COMPANY_SESSION_KEY] == company_id

        second = client.get(path)
        assert second.status_code == 302


def test_revoking_company_membership_also_revokes_branch_access(app):
    from app import Branch, UserBranchAccess

    with app.app_context():
        company = _company("Empresa con sucursales")
        branch_one = Branch(
            company_id=company.id, name="Casa central", code="CASA",
            active=True, is_default=True,
        )
        branch_two = Branch(
            company_id=company.id, name="Norte", code="NORTE",
            active=True, is_default=False,
        )
        db.session.add_all([branch_one, branch_two])
        db.session.flush()
        user = _user(company, username="branch-revocation-user")
        membership = _membership(
            user, company, role="user", permissions='["inventory"]'
        )
        db.session.add_all([
            UserBranchAccess(
                company_id=company.id, user_id=user.id, branch_id=branch_one.id
            ),
            UserBranchAccess(
                company_id=company.id, user_id=user.id, branch_id=branch_two.id
            ),
        ])
        db.session.commit()

        membership.active = False
        UserBranchAccess.query.filter_by(
            company_id=company.id, user_id=user.id
        ).delete(synchronize_session=False)
        db.session.commit()

        assert UserBranchAccess.query.filter_by(
            company_id=company.id, user_id=user.id
        ).count() == 0
